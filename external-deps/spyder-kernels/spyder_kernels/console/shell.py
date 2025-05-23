# -*- coding: utf-8 -*-
# -----------------------------------------------------------------------------
# Copyright (c) 2009- Spyder Kernels Contributors
#
# Licensed under the terms of the MIT License
# (see spyder_kernels/__init__.py for details)
# -----------------------------------------------------------------------------

"""
Spyder shell for Jupyter kernels.
"""

# Standard library imports
import bdb
import itertools
import logging
import os
import re
import signal
import sys
import traceback
from _thread import interrupt_main
from typing import List

# Third-party imports
from ipykernel.zmqshell import ZMQInteractiveShell

# Local imports
from spyder_kernels.customize.namespace_manager import NamespaceManager
from spyder_kernels.customize.spyderpdb import SpyderPdb
from spyder_kernels.customize.code_runner import SpyderCodeRunner
from spyder_kernels.comms.commbase import stacksummary_to_json
from spyder_kernels.comms.decorators import comm_handler
from spyder_kernels.utils.mpl import automatic_backend


logger = logging.getLogger(__name__)


class SpyderShell(ZMQInteractiveShell):
    """Spyder shell."""

    PDB_CONF_KEYS = [
        'pdb_ignore_lib',
        'pdb_execute_events',
        'pdb_use_exclamation_mark',
        'pdb_stop_first_line',
        'breakpoints',
        'pdb_publish_stack'
    ]

    def __init__(self, *args, **kwargs):
        # Create _namespace_stack before __init__
        self._namespace_stack = []
        self._request_pdb_stop = False
        self.special = None
        self._pdb_conf = {}
        super(SpyderShell, self).__init__(*args, **kwargs)
        self._allow_kbdint = False
        self.register_debugger_sigint()
        self.update_gui_frontend = False
        self._spyder_theme = 'dark'

        # Substrings of the directory where Spyder-kernels is installed
        self._package_locations = [
            # When the package is properly installed
            os.path.join("site-packages", "spyder_kernels"),
            # When it's installed from the external-deps subrepo. We need this
            # for our tests
            os.path.join("external-deps", "spyder-kernels", "spyder_kernels")
        ]

        # register post_execute
        self.events.register('post_execute', self.do_post_execute)

        # Disable Python package managers because they don't work reliably for
        # us when called from the kernel.
        self._disabled_pkg_managers = [
            "conda",
            "mamba",
            "micromamba",
            "pip",
            "pixi",
            "uv",
        ]
        self._disable_pkg_managers_msg = (
            "\\nInstalling packages through the IPython console doesn't work "
            "reliably in Spyder. Please use a system terminal to do that, "
            "i.e. cmd.exe on Windows, Terminal on macOS or xterm on "
            "Linux."
        )
        self.input_transformers_cleanup.append(self._do_input_cleanup)

    def init_magics(self):
        """Init magics"""
        super().init_magics()
        self.register_magics(SpyderCodeRunner)

    def ask_exit(self):
        """Engage the exit actions."""
        if self.active_eventloop not in [None, "inline"]:
            # Some eventloops prevent the kernel from shutting down
            self.enable_gui('inline')
        super().ask_exit()

    def _showtraceback(self, etype, evalue, stb):
        """Handle how tracebacks are displayed in the console."""
        spyder_stb = []
        if etype is bdb.BdbQuit:
            # Don't show a traceback when exiting our debugger after entering
            # it through a `breakpoint()` call. This is because calling `!exit`
            # after `breakpoint()` raises BdbQuit, which throws a long and
            # useless traceback.
            spyder_stb.append('')
        else:
            # Skip internal frames from the traceback's string representation
            for line in stb:
                if (
                    # Verbose mode
                    re.match(r"File (.*)", line)
                    # Plain mode
                    or re.match(r"\x1b\[(.*)  File (.*)", line)
                ) and (
                    # The file line should not contain a location where
                    # Spyder-kernels is installed
                    any(
                        [
                            location in line
                            for location in self._package_locations
                        ]
                    )
                ):
                    continue
                else:
                    spyder_stb.append(line)

        super()._showtraceback(etype, evalue, spyder_stb)

    def set_spyder_theme(self, theme):
        """Set the theme for the console."""
        self._spyder_theme = theme
        if theme == "dark":
            # Needed to change the colors of tracebacks
            self.run_line_magic("colors", "linux")
        elif theme == "light":
            self.run_line_magic("colors", "lightbg")

    def get_spyder_theme(self):
        """Get the theme for the console."""
        return self._spyder_theme

    def enable_matplotlib(self, gui=None):
        """Enable matplotlib."""
        if gui is None or gui.lower() == "auto":
            gui = automatic_backend()

        # Before activating the backend, restore to file default those
        # InlineBackend settings that may have been set explicitly.
        self.kernel.restore_rc_file_defaults()

        enabled_gui, backend = super().enable_matplotlib(gui)

        # This is necessary for IPython 8.24+, which returns None after
        # enabling the Inline backend.
        if enabled_gui is None and gui == "inline":
            enabled_gui = "inline"
        gui = enabled_gui

        # Check if the inline backend is registered. It should be at this
        # point, but sometimes that can fail due to a mismatch between
        # the installed versions of IPython, matplotlib and matplotlib-inline.
        # Fixes spyder-ide/spyder#22420.
        if gui == "inline":
            is_inline_registered = False

            # The flush_figures callback should be listed as a post_execute
            # event if the backend was registered successfully.
            for event in self.events.callbacks["post_execute"]:
                if "matplotlib_inline.backend_inline.flush_figures" in repr(
                    event
                ):
                    is_inline_registered = True
                    break

            # Manually register the backend in case it wasn't
            if not is_inline_registered:
                from IPython.core.pylabtools import activate_matplotlib
                from matplotlib_inline.backend_inline import (
                    configure_inline_support
                )

                backend = "module://matplotlib_inline.backend_inline"
                activate_matplotlib(backend)
                configure_inline_support(self, backend)

        # To easily track the current interactive backend
        if self.kernel.interactive_backend is None:
            self.kernel.interactive_backend = gui if gui != "inline" else None

        if self.update_gui_frontend:
            try:
                self.kernel.frontend_call(
                    blocking=False
                ).update_matplotlib_gui(gui)
            except Exception:
                pass

        return gui, backend

    # --- For Pdb namespace integration
    def set_pdb_configuration(self, pdb_conf):
        """
        Set Pdb configuration.

        Parameters
        ----------
        pdb_conf: dict
            Dictionary containing the configuration. Its keys are part of the
            `PDB_CONF_KEYS` class constant.
        """
        for key in self.PDB_CONF_KEYS:
            if key in pdb_conf:
                self._pdb_conf[key] = pdb_conf[key]
                if self.pdb_session:
                    setattr(self.pdb_session, key, pdb_conf[key])

    def is_debugging(self):
        """
        Check if we are currently debugging.
        """
        for session in self._namespace_stack[::-1]:
            if isinstance(session, SpyderPdb) and session.curframe is not None:
                return True
        return False

    @property
    def pdb_session(self):
        """Get current pdb session."""
        for session in self._namespace_stack[::-1]:
            if isinstance(session, SpyderPdb):
                return session
        return None

    def add_pdb_session(self, pdb_obj):
        """Add a pdb object to the stack."""
        if self.pdb_session == pdb_obj:
            # Already added
            return
        self._namespace_stack.append(pdb_obj)

        # Set config to pdb obj
        self.set_pdb_configuration(self._pdb_conf)

    def remove_pdb_session(self, pdb_obj):
        """Remove a pdb object from the stack."""
        if self.pdb_session != pdb_obj:
            # Already removed
            return
        self._namespace_stack.pop()

        if self.pdb_session:
            # Set config to newly active pdb obj
            self.set_pdb_configuration(self._pdb_conf)

    def add_namespace_manager(self, ns_manager):
        """Add namespace manager to stack."""
        self._namespace_stack.append(ns_manager)

    def remove_namespace_manager(self, ns_manager):
        """Remove namespace manager."""
        if self._namespace_stack[-1] != ns_manager:
            logger.debug("The namespace stack is inconsistent.")
            return
        self._namespace_stack.pop()

    def get_local_scope(self, stack_depth):
        """
        Get local scope at a given frame depth.

        Needed for magics that use "needs_local_scope" such as timeit
        """
        frame = sys._getframe(stack_depth + 1)
        return self.context_locals(frame)

    def context_locals(self, frame=None):
        """
        Get context locals.

        If frame is not None, make sure frame.f_locals is not registered in a
        debugger and return frame.f_locals
        """
        for session in self._namespace_stack[::-1]:
            if isinstance(session, SpyderPdb) and session.curframe is not None:
                if frame is None or frame == session.curframe:
                    return session.curframe_locals
            elif frame is None and isinstance(session, NamespaceManager):
                return session.ns_locals

        if frame is not None:
            return frame.f_locals

        return None

    @property
    def _pdb_frame(self):
        """Return current Pdb frame if there is any"""
        if self.pdb_session is not None:
            return self.pdb_session.curframe

    @property
    def user_ns(self):
        """Get the current namespace."""
        for session in self._namespace_stack[::-1]:
            if isinstance(session, SpyderPdb) and session.curframe is not None:
                # Return first debugging namespace
                return session.curframe.f_globals
            elif isinstance(session, NamespaceManager):
                return session.ns_globals

        return self.__user_ns

    @user_ns.setter
    def user_ns(self, namespace):
        """Set user_ns."""
        self.__user_ns = namespace

    def _get_current_namespace(self, with_magics=False, frame=None):
        """Return a copy of the current namespace."""
        if frame is not None:
            ns = frame.f_globals.copy()
            ns.update(self.context_locals(frame))
            return ns

        ns = {}
        ns.update(self.user_ns)
        context_locals = self.context_locals()
        if context_locals:
            ns.update(context_locals)

        # Add magics to ns so we can show help about them on the Help
        # plugin
        if with_magics:
            line_magics = self.magics_manager.magics['line']
            cell_magics = self.magics_manager.magics['cell']
            ns.update(line_magics)
            ns.update(cell_magics)

        return ns

    def _get_reference_namespace(self, name):
        """
        Return namespace where reference name is defined

        It returns the user namespace if name has not yet been defined.
        """
        lcls = self.context_locals()
        if lcls and name in lcls:

            return lcls
        return self.user_ns

    def showtraceback(self, exc_tuple=None, filename=None, tb_offset=None,
                      exception_only=False, running_compiled_code=False):
        """Display the exception that just occurred."""
        super(SpyderShell, self).showtraceback(
            exc_tuple, filename, tb_offset,
            exception_only, running_compiled_code)
        if not exception_only:
            try:
                etype, value, tb = self._get_exc_info(exc_tuple)
                etype = etype.__name__
                value = value.args
                stack = stacksummary_to_json(traceback.extract_tb(tb.tb_next))
                self.kernel.frontend_call(blocking=False).show_traceback(
                    etype, value, stack)
            except Exception:
                return

    def register_debugger_sigint(self):
        """Register sigint handler."""
        signal.signal(signal.SIGINT, self.spyderkernel_sigint_handler)

    @comm_handler
    def raise_interrupt_signal(self):
        """Raise interrupt signal."""
        if os.name == "nt":
            # Check if signal handler is callable to avoid
            # 'int not callable' error (Python issue #23395)
            if callable(signal.getsignal(signal.SIGINT)):
                interrupt_main()
            else:
                self.kernel.log.error(
                    "Interrupt message not supported on Windows")
        else:
            # This is necessary to make the call below work for IPykernel
            # versions equal or less than 6.21.2 and greater than it.
            # See ipython/ipykernel#1101
            if hasattr(self.kernel, '_send_interupt_children'):
                self.kernel._send_interupt_children()
            else:
                self.kernel._send_interrupt_children()

    @comm_handler
    def request_pdb_stop(self):
        """Request pdb to stop at the next possible position."""
        pdb_session = self.pdb_session
        if pdb_session:
            if pdb_session.interrupting:
                # interrupt already requested, wait
                return
            # trace_dispatch is active, stop at the next possible position
            pdb_session.interrupt()
        elif (self.spyderkernel_sigint_handler
              == signal.getsignal(signal.SIGINT)):
            # Use spyderkernel_sigint_handler
            self._request_pdb_stop = True
            self.raise_interrupt_signal()
        else:
            logger.debug(
                "Can not signal main thread to stop as SIGINT "
                "handler was replaced and the debugger is not active. "
                "The current handler is: " +
                repr(signal.getsignal(signal.SIGINT))
            )

    def spyderkernel_sigint_handler(self, signum, frame):
        """SIGINT handler."""
        if self._request_pdb_stop:
            # SIGINT called from request_pdb_stop
            self._request_pdb_stop = False
            debugger = SpyderPdb()
            debugger.interrupt()
            debugger.set_trace(frame)
            return

        pdb_session = self.pdb_session
        if pdb_session:
            # SIGINT called while debugging
            if pdb_session.allow_kbdint:
                raise KeyboardInterrupt
            if pdb_session.interrupting:
                # second call to interrupt, raise
                raise KeyboardInterrupt
            pdb_session.interrupt()
            return

        if self._allow_kbdint:
            # Do not raise KeyboardInterrupt in the middle of ipython code
            raise KeyboardInterrupt

    async def run_code(self, *args, **kwargs):
        """Execute a code object."""
        try:
            try:
                self._allow_kbdint = True
                return await super().run_code(*args, **kwargs)
            finally:
                self._allow_kbdint = False
        except KeyboardInterrupt:
            self.showtraceback()

    @comm_handler
    def pdb_input_reply(self, line, echo_stack_entry=True):
        """Get a pdb command from the frontend."""
        debugger = self.pdb_session
        if not debugger:
            return
        debugger._disable_next_stack_entry = not echo_stack_entry
        debugger._cmd_input_line = line
        # Interrupts eventloop if needed
        self.kernel.interrupt_eventloop()

    def do_post_execute(self):
        """Flush __std*__ after execution."""
        # Flush C standard streams.
        sys.__stderr__.flush()
        sys.__stdout__.flush()
        self.kernel.publish_state()

    def _do_input_cleanup(self, lines: List[str]):
        """
        Input transformations before the code is made valid Python by IPython.
        """
        for line in lines:
            # Disable magics and commands to call Python package managers from
            # the kernel because they don't work reliably.
            # Fixes spyder-ide/spyder#21894
            if any(
                [
                    line.startswith(f"{prefix}{command}")
                    for prefix, command in itertools.product(
                        ["%", "!"], self._disabled_pkg_managers
                    )
                ]
            ):
                return [f'print("{self._disable_pkg_managers_msg}")']

        return lines
