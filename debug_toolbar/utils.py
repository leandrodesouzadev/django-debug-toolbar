import inspect
import linecache
import os.path
import sys
import warnings
from pprint import pformat
from typing import Any, Dict, List, Optional, Sequence, Tuple

from asgiref.local import Local
from django.template import Node
from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe

from debug_toolbar import settings as dt_settings, stubs

try:
    import threading
except ImportError:
    threading = None


_local_data = Local()


def _is_excluded_frame(frame: Any, excluded_modules: Optional[Sequence[str]]) -> bool:
    if not excluded_modules:
        return False
    frame_module = frame.f_globals.get("__name__")
    if not isinstance(frame_module, str):
        return False
    return any(
        frame_module == excluded_module
        or frame_module.startswith(excluded_module + ".")
        for excluded_module in excluded_modules
    )


def _stack_trace_deprecation_warning() -> None:
    warnings.warn(
        "get_stack() and tidy_stacktrace() are deprecated in favor of"
        " get_stack_trace()",
        DeprecationWarning,
        stacklevel=2,
    )


def tidy_stacktrace(stack: List[stubs.InspectStack]) -> stubs.TidyStackTrace:
    """
    Clean up stacktrace and remove all entries that are excluded by the
    HIDE_IN_STACKTRACES setting.

    ``stack`` should be a list of frame tuples from ``inspect.stack()`` or
    ``debug_toolbar.utils.get_stack()``.
    """
    _stack_trace_deprecation_warning()

    trace = []
    excluded_modules = dt_settings.get_config()["HIDE_IN_STACKTRACES"]
    for frame, path, line_no, func_name, text in (f[:5] for f in stack):
        if _is_excluded_frame(frame, excluded_modules):
            continue
        text = "".join(text).strip() if text else ""
        frame_locals = (
            frame.f_locals
            if dt_settings.get_config()["ENABLE_STACKTRACES_LOCALS"]
            else None
        )
        trace.append((path, line_no, func_name, text, frame_locals))
    return trace


def render_stacktrace(trace: stubs.TidyStackTrace) -> SafeString:
    show_locals = dt_settings.get_config()["ENABLE_STACKTRACES_LOCALS"]
    html = ""
    for abspath, lineno, func, code, locals_ in trace:
        if os.path.sep in abspath:
            directory, filename = abspath.rsplit(os.path.sep, 1)
            # We want the separator to appear in the UI so add it back.
            directory += os.path.sep
        else:
            # abspath could be something like "<frozen importlib._bootstrap>"
            directory = ""
            filename = abspath
        html += format_html(
            (
                '<span class="djdt-path">{}</span>'
                + '<span class="djdt-file">{}</span> in'
                + ' <span class="djdt-func">{}</span>'
                + '(<span class="djdt-lineno">{}</span>)\n'
                + '  <span class="djdt-code">{}</span>\n'
            ),
            directory,
            filename,
            func,
            lineno,
            code,
        )
        if show_locals:
            html += format_html(
                '  <pre class="djdt-locals">{}</pre>\n',
                pformat(locals_),
            )
        html += "\n"
    return mark_safe(html)


def get_template_info() -> Optional[Dict[str, Any]]:
    template_info = None
    cur_frame = sys._getframe().f_back
    try:
        while cur_frame is not None:
            in_utils_module = cur_frame.f_code.co_filename.endswith(
                "/debug_toolbar/utils.py"
            )
            is_get_template_context = (
                cur_frame.f_code.co_name == get_template_context.__name__
            )
            if in_utils_module and is_get_template_context:
                # If the method in the stack trace is this one
                # then break from the loop as it's being check recursively.
                break
            elif cur_frame.f_code.co_name == "render":
                node = cur_frame.f_locals["self"]
                context = cur_frame.f_locals["context"]
                if isinstance(node, Node):
                    template_info = get_template_context(node, context)
                    break
            cur_frame = cur_frame.f_back
    except Exception:
        pass
    del cur_frame
    return template_info


def get_template_context(node, context, context_lines=3) -> Dict[str, Any]:
    line, source_lines, name = get_template_source_from_exception_info(node, context)
    debug_context = []
    start = max(1, line - context_lines)
    end = line + 1 + context_lines

    for line_num, content in source_lines:
        if start <= line_num <= end:
            debug_context.append(
                {"num": line_num, "content": content, "highlight": (line_num == line)}
            )

    return {"name": name, "context": debug_context}


def get_template_source_from_exception_info(
    node: Node, context: stubs.RequestContext
) -> Tuple[int, List[Tuple[int, str]], str]:
    if context.template.origin == node.origin:
        exception_info = context.template.get_exception_info(
            Exception("DDT"), node.token
        )
    else:
        exception_info = context.render_context.template.get_exception_info(
            Exception("DDT"), node.token
        )
    line = exception_info["line"]
    source_lines = exception_info["source_lines"]
    name = exception_info["name"]
    return line, source_lines, name


def get_name_from_obj(obj: Any) -> str:
    if hasattr(obj, "__name__"):
        name = obj.__name__
    else:
        name = obj.__class__.__name__

    if hasattr(obj, "__module__"):
        module = obj.__module__
        name = f"{module}.{name}"

    return name


def getframeinfo(frame: Any, context=1) -> inspect.Traceback:
    """
    Get information about a frame or traceback object.

    A tuple of five things is returned: the filename, the line number of
    the current line, the function name, a list of lines of context from
    the source code, and the index of the current line within that list.
    The optional second argument specifies the number of lines of context
    to return, which are centered around the current line.

    This originally comes from ``inspect`` but is modified to handle issues
    with ``findsource()``.
    """
    if inspect.istraceback(frame):
        lineno = frame.tb_lineno
        frame = frame.tb_frame
    else:
        lineno = frame.f_lineno
    if not inspect.isframe(frame):
        raise TypeError("arg is not a frame or traceback object")

    filename = inspect.getsourcefile(frame) or inspect.getfile(frame)
    if context > 0:
        start = lineno - 1 - context // 2
        try:
            lines, lnum = inspect.findsource(frame)
        except Exception:  # findsource raises platform-dependant exceptions
            lines = index = None
        else:
            start = max(start, 1)
            start = max(0, min(start, len(lines) - context))
            lines = lines[start : (start + context)]
            index = lineno - 1 - start
    else:
        lines = index = None

    return inspect.Traceback(filename, lineno, frame.f_code.co_name, lines, index)


def get_sorted_request_variable(variable):
    """
    Get a data structure for showing a sorted list of variables from the
    request data.
    """
    try:
        if isinstance(variable, dict):
            return {"list": [(k, variable.get(k)) for k in sorted(variable)]}
        else:
            return {"list": [(k, variable.getlist(k)) for k in sorted(variable)]}
    except TypeError:
        return {"raw": variable}


def get_stack(context=1) -> List[stubs.InspectStack]:
    """
    Get a list of records for a frame and all higher (calling) frames.

    Each record contains a frame object, filename, line number, function
    name, a list of lines of context, and index within the context.

    Modified version of ``inspect.stack()`` which calls our own ``getframeinfo()``
    """
    _stack_trace_deprecation_warning()

    frame = sys._getframe(1)
    framelist = []
    while frame:
        framelist.append((frame,) + getframeinfo(frame, context))
        frame = frame.f_back
    return framelist


def _stack_frames(*, skip=0):
    skip += 1  # Skip the frame for this generator.
    frame = inspect.currentframe()
    while frame is not None:
        if skip > 0:
            skip -= 1
        else:
            yield frame
        frame = frame.f_back


class _StackTraceRecorder:
    def __init__(self):
        self.filename_cache = {}

    def get_source_file(self, frame):
        frame_filename = frame.f_code.co_filename

        value = self.filename_cache.get(frame_filename)
        if value is None:
            filename = inspect.getsourcefile(frame)
            if filename is None:
                is_source = False
                filename = frame_filename
            else:
                is_source = True
                # Ensure linecache validity the first time this recorder
                # encounters the filename in this frame.
                linecache.checkcache(filename)
            value = (filename, is_source)
            self.filename_cache[frame_filename] = value

        return value

    def get_stack_trace(
        self,
        *,
        excluded_modules: Optional[Sequence[str]] = None,
        include_locals: bool = False,
        skip: int = 0,
    ):
        trace = []
        skip += 1  # Skip the frame for this method.
        for frame in _stack_frames(skip=skip):
            if _is_excluded_frame(frame, excluded_modules):
                continue

            filename, is_source = self.get_source_file(frame)

            line_no = frame.f_lineno
            func_name = frame.f_code.co_name

            if is_source:
                module = inspect.getmodule(frame, filename)
                module_globals = module.__dict__ if module is not None else None
                source_line = linecache.getline(
                    filename, line_no, module_globals
                ).strip()
            else:
                source_line = ""

            frame_locals = frame.f_locals if include_locals else None

            trace.append((filename, line_no, func_name, source_line, frame_locals))
        trace.reverse()
        return trace


def get_stack_trace(*, skip=0):
    """
    Return a processed stack trace for the current call stack.

    If the ``ENABLE_STACKTRACES`` setting is False, return an empty :class:`list`.
    Otherwise return a :class:`list` of processed stack frame tuples (file name, line
    number, function name, source line, frame locals) for the current call stack.  The
    first entry in the list will be for the bottom of the stack and the last entry will
    be for the top of the stack.

    ``skip`` is an :class:`int` indicating the number of stack frames above the frame
    for this function to omit from the stack trace.  The default value of ``0`` means
    that the entry for the caller of this function will be the last entry in the
    returned stack trace.
    """
    config = dt_settings.get_config()
    if not config["ENABLE_STACKTRACES"]:
        return []
    skip += 1  # Skip the frame for this function.
    stack_trace_recorder = getattr(_local_data, "stack_trace_recorder", None)
    if stack_trace_recorder is None:
        stack_trace_recorder = _StackTraceRecorder()
        _local_data.stack_trace_recorder = stack_trace_recorder
    return stack_trace_recorder.get_stack_trace(
        excluded_modules=config["HIDE_IN_STACKTRACES"],
        include_locals=config["ENABLE_STACKTRACES_LOCALS"],
        skip=skip,
    )


def clear_stack_trace_caches():
    if hasattr(_local_data, "stack_trace_recorder"):
        del _local_data.stack_trace_recorder


class ThreadCollector:
    def __init__(self):
        if threading is None:
            raise NotImplementedError(
                "threading module is not available, "
                "this panel cannot be used without it"
            )
        self.collections = {}  # a dictionary that maps threads to collections

    def get_collection(self, thread=None):
        """
        Returns a list of collected items for the provided thread, of if none
        is provided, returns a list for the current thread.
        """
        if thread is None:
            thread = threading.current_thread()
        if thread not in self.collections:
            self.collections[thread] = []
        return self.collections[thread]

    def clear_collection(self, thread=None):
        if thread is None:
            thread = threading.current_thread()
        if thread in self.collections:
            del self.collections[thread]

    def collect(self, item, thread=None):
        self.get_collection(thread).append(item)
