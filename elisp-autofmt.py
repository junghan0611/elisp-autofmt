#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later

'''
Emacs lisp auto formatter.
'''

from __future__ import annotations
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    Iterable,
    List,
    NamedTuple,
    Optional,
    Set,
    Sequence,
    TextIO,
    Tuple,
    Union,
)

import sys
import os
import argparse

HintType = Dict[str, Union[str, int, Tuple[int, int]]]
NdSexp_WrapState = Tuple[bool, ...]

__all__ = (
    'main',
)


# ------------------------------------------------------------------------------
# Globals

# Disable mainly for testing what happens when we don't wrap lines at all.
USE_WRAP_LINES = True

# For debugging, check who added the newline.
USE_DEBUG_TRACE_NEWLINES = False

# Extract argument count from functions and macros.
USE_EXTRACT_DEFS = True

# Report missing definitions.
LOG_MISSING_DEFS = None  # '/tmp/out.log'

# Asserts that incur performance penalties which should not be enabled by default.
USE_PARANOID_ASSERT = False


# ------------------------------------------------------------------------------
# Exceptions

# Exception for failure to parse the file,
# show this in the command line output.
class FmtException(Exception):
    '''
    An exception raised for malformed files, where formatting cannot complete.
    '''


class FmtExceptionEarlyExit(Exception):
    '''
    Early exit from within callbacks.
    '''


# ------------------------------------------------------------------------------
# Utilities

def is_hash_prefix_special_case(text: str) -> bool:
    '''
    Return true if the hash should be connected to the following text.
    '''
    return (
        text.startswith('#') and
        (not text.startswith('#\''))
    )


# ------------------------------------------------------------------------------
# Line Length Checks

def calc_over_long_line_score(data: str, fill_column: int, trailing_parens: int, line_terminate: int) -> int:
    '''
    The resulting score is zero when all values are within the fill column.
    Otherwise a score will be returned which is used to compare the state of wrapped lines
    (bigger is worse).
    '''

    # This is the accumulated ``2 ** overflow``.
    # Note that the power is used so breaking a single line into two which both overflow
    # return a better (lower) score than a single line that overflows.

    # Step over `\n` characters instead of `data.split('\n')`
    # so multiple characters are handled separately.
    line_step = 0
    i = 0

    score = 0
    if line_terminate != -1:
        while line_step != -1:
            line_step_next = data.find('\n', line_step)
            if line_step_next == -1:
                line_length = len(data) - line_step
                line_step = -1
            else:
                line_length = line_step_next - line_step
                line_step = line_step_next + 1

            if line_terminate == i:
                line_length += trailing_parens
                if line_length > fill_column:
                    score += 2 ** (line_length - fill_column)
                break
            if line_length > fill_column:
                score += 2 ** (line_length - fill_column)
            i += 1
    else:
        while line_step != -1:
            line_step_next = data.find('\n', line_step)
            if line_step_next == -1:
                line_length = len(data) - line_step
                line_step = -1
            else:
                line_length = line_step_next - line_step
                line_step = line_step_next + 1

            if line_length > fill_column:
                score += 2 ** (line_length - fill_column)
            i += 1

    return score


def calc_over_long_line_length_test(data: str, fill_column: int, trailing_parens: int, line_terminate: int) -> int:
    '''
    Return zero when all lines are within the ``fill_column``, otherwise 1.
    '''

    # Step over `\n` characters instead of `data.split('\n')`
    # so multiple characters are handled separately.
    line_step = 0
    i = 0

    if line_terminate != -1:
        while line_step != -1:
            line_step_next = data.find('\n', line_step)
            if line_step_next == -1:
                line_length = len(data) - line_step
                line_step = -1
            else:
                line_length = line_step_next - line_step
                line_step = line_step_next + 1

            if line_terminate == i:
                line_length += trailing_parens
                if line_length > fill_column:
                    return 1
                break
            if line_length > fill_column:
                return 1
            i += 1
    else:
        while line_step != -1:
            line_step_next = data.find('\n', line_step)
            if line_step_next == -1:
                line_length = len(data) - line_step
                line_step = -1
            else:
                line_length = line_step_next - line_step
                line_step = line_step_next + 1

            if line_length > fill_column:
                return 1
            i += 1

    return 0


# ------------------------------------------------------------------------------
# Formatting Utilities

def apply_comment_force_newline(root: NdSexp) -> None:
    '''
    Ensure new-lines are properly inserted
    to prevent code being placed at the end of a comment (turning it into a comment).
    '''

    # Special calculation for lines with comments on same line,
    # don't merge these lines since it would make it seem as if
    # the comment applies to the statements on that line.
    if not root.nodes:
        return

    node_line_start = root.nodes[0]
    for node, node_parent in root.iter_nodes_recursive_with_parent():
        if node is node_line_start:
            continue
        if isinstance(node, NdComment) and node.is_own_line is False:
            if not isinstance(node_line_start, NdWs):
                if node_line_start.force_newline:
                    pass
                elif node_line_start is node_parent.nodes[0]:
                    node_parent.force_newline = True
                else:
                    node_line_start.force_newline = True
        if node_line_start.original_line != node.original_line:
            node_line_start = node


def apply_relaxed_wrap(node_parent: NdSexp, style: FmtStyle) -> None:
    '''
    Wrap nodes in ``node_parent``, without taking the fill column into account.
    Some rules of thumb are applied such as:
    - Grouping :key value pairs.
    - Grouping as a hint (needed for ``setq`` argument pairing).

    This is done since every argument having it's own line is often not what users want,
    especially for keyword-value pairs. So it's best to first perform a relaxed wrap,
    then only further wrap arguments if they exceed the fill-column (which must be done as a separate step).
    '''

    node_prev = None
    force_newline = False

    if node_parent.hints is not None:
        hint_group = node_parent.hints.get('group')
    else:
        hint_group = None

    if hint_group is not None:
        assert isinstance(hint_group, list) and len(hint_group) == 2
        group_beg, group_len = hint_group
        assert isinstance(group_beg, int)
        assert isinstance(group_len, int)

        nodes_iter = node_parent.nodes_only_code[group_beg + 1:]
    else:
        nodes_iter = node_parent.nodes_only_code[node_parent.index_wrap_hint:]

    i_last = len(nodes_iter) - 1

    # Make a map, we could store this for reuse.
    nodes_with_trailing_comment_or_newline = set()
    node_prev = None
    for node in node_parent.nodes:
        if isinstance(node, (NdComment, NdWs)):
            nodes_with_trailing_comment_or_newline.add(id(node_prev))
        node_prev = node
    # Finish building 'nodes_with_trailing_comment_or_newline'.

    if hint_group is not None:
        group_len = hint_group[1]
        for i, node in enumerate(nodes_iter):
            ok = True

            if (i % group_len) != 0:
                ok = False

            if ok:
                node.force_newline = True
            force_newline |= node.force_newline

            node_prev = node

    else:
        for i, node in enumerate(nodes_iter):
            node_next = None if i == i_last else nodes_iter[i + 1]

            ok = True
            # Keep pairs:
            #     (foo
            #       :keyword value)
            #
            # Or:
            #     (foo
            #       :keyword value
            #       :other other-value)
            #
            # But only pairs, so multiple values each get their own line:
            #     (foo
            #       :keyword
            #       value
            #       other-value)
            #
            # .. Better for use-package 'config' sections.
            #
            # But keep their pairs in the case of a blank line or comment.
            #     (foo
            #       :keyword value
            #
            #       other-value)
            #
            # .. Useful for macros such as 'define-minor-mode' which have properties, then a &rest.
            #
            if (
                    (isinstance(node_prev, NdSymbol) and node_prev.data.startswith(':')) and
                    (
                        (node_next is None) or
                        (id(node) in nodes_with_trailing_comment_or_newline) or
                        (isinstance(node_next, NdSymbol) and node_next.data.startswith(':'))
                    )
            ):
                ok = False

            if ok:
                node.force_newline = True
            force_newline |= node.force_newline

            node_prev = node

    if not style.use_native:
        if force_newline:
            node_parent.force_newline = True


def apply_relaxed_wrap_when_multiple_args(node_parent: NdSexp, style: FmtStyle) -> None:
    '''
    Relaxed wrap when the S-expression as 2 or more arguments passed in.
    '''
    if len(node_parent.nodes_only_code) - node_parent.index_wrap_hint > 1:
        apply_relaxed_wrap(node_parent, style)


def parse_local_defs(defs: FmtDefs, node_parent: NdSexp) -> None:
    '''
    Extract definitions from the file being formatted.

    While it's possible to store definitions for all files and load them in from JSON,
    this isn't practical when the file being edited would have to re-generate definitions
    every time.

    So extract definitions from ourselves (function properties and function argument counts).
    '''
    # Extract the number of functions from local definitions.
    # Currently only used so we can break the number of arguments at '&rest'
    # since it's nearly always where we have the body of macros which is logically
    # where we want to break.
    if node_parent.nodes_only_code and node_parent.brackets == '()':
        node = node_parent.nodes_only_code[0]
        if isinstance(node, NdSymbol):
            symbol_type = None
            if node.data in {
                    'defsubst',
                    'defun',
                    'defadvice',
                    'iter-defun',
            }:
                symbol_type = 'func'
            elif node.data in {
                    'defmacro',
            }:
                symbol_type = 'macro'
            else:
                return

            # Sanity check, should never fail.
            if len(node_parent.nodes_only_code) >= 3:
                node_symbol = node_parent.nodes_only_code[1]
                node_args = node_parent.nodes_only_code[2]
                if isinstance(node_symbol, NdSymbol) and isinstance(node_args, NdSexp):
                    symbol = node_symbol.data
                    arg_index_min = 0
                    arg_index_max: Union[int, str] = 0
                    hints: Optional[HintType] = None
                    for i, node_arg in enumerate(node_args.nodes_only_code):
                        if not isinstance(node_arg, NdSymbol):
                            continue

                        if node_arg.data == '&rest':
                            arg_index_min = i
                            arg_index_max = 'many'
                            break
                        if node_arg.data == '&optional':
                            arg_index_min = i
                            arg_index_max = i
                            # Count remaining arguments and exit.
                            for j in range(i + 1, len(node_args.nodes_only_code)):
                                if isinstance(node_args.nodes_only_code[j], NdSymbol):
                                    arg_index_max = j
                            break

                    if node.data in {'defun', 'defmacro'}:
                        if len(node_parent.nodes_only_code) >= 4:
                            node_decl = None
                            if isinstance(node_parent.nodes_only_code[3], NdString):
                                if len(node_parent.nodes_only_code) >= 5:
                                    if isinstance(node_parent.nodes_only_code[4], NdSexp):
                                        node_decl = node_parent.nodes_only_code[4]
                            else:
                                if isinstance(node_parent.nodes_only_code[3], NdSexp):
                                    node_decl = node_parent.nodes_only_code[3]

                            # First argument after a function may be 'declare'.
                            if node_decl is not None and len(node_decl.nodes_only_code) > 1:
                                if (
                                        (isinstance(node_decl.nodes_only_code[0], NdSymbol)) and
                                        (node_decl.nodes_only_code[0].data == 'declare')
                                ):
                                    # The second value is currently unused (max arguments).
                                    for node_iter in node_decl.nodes_only_code[1:]:
                                        if (
                                                isinstance(node_iter, NdSexp) and
                                                len(node_iter.nodes_only_code) == 2
                                        ):
                                            node_key, node_val = node_iter.nodes_only_code
                                            if isinstance(node_key, NdSymbol):
                                                key = node_key.data
                                                if key == 'indent':
                                                    if hints is None:
                                                        hints = {}
                                                    if isinstance(node_val, NdSymbol):
                                                        val = node_val.data
                                                        hints[key] = int(val) if val.isdigit() else val
                                                elif key == 'doc-string':
                                                    if hints is None:
                                                        hints = {}
                                                    if isinstance(node_val, NdSymbol):
                                                        val = node_val.data
                                                        hints[key] = int(val) if val.isdigit() else val

                    defs.fn_arity[symbol] = FnArity(
                        symbol_type=symbol_type,
                        nargs_min=arg_index_min,
                        nargs_max=arg_index_max,
                        hints=hints,
                    )

    for node in node_parent.nodes_only_code:
        if isinstance(node, NdSexp):
            parse_local_defs(defs, node)


def scan_used_fn_defs(defs: FmtDefs, node_parent: NdSexp, fn_used: Set[str]) -> None:
    '''
    Fill ``fn_used`` with a list of definitions used in this document.
    Used to implement ``FmtDefs.prune_unused``.
    '''
    if node_parent.nodes_only_code:
        node = node_parent.nodes_only_code[0]
        if isinstance(node, NdSymbol):
            symbol = node.data
            len_prev = len(fn_used)
            fn_used.add(symbol)
            if len_prev != len(fn_used):
                # These wont come from local definitions (only ones generated by Emacs)
                # so it's save to use this as a lookup even though this data is being populated.
                fn_data = defs.fn_arity.get(symbol)
                if fn_data is not None:
                    hints: Optional[HintType] = fn_data[3]
                    if hints:
                        for hint_key in ('doc-string', 'indent'):
                            hint_value = hints.get(hint_key)
                            if isinstance(hint_value, str):
                                fn_used.add(hint_value)
    for node in node_parent.nodes_only_code:
        if isinstance(node, NdSexp):
            scan_used_fn_defs(defs, node, fn_used)


def apply_rules(cfg: FmtConfig, node_parent: NdSexp) -> None:
    '''
    Define line breaks using rules set by:

    - Function properties.
    - Function argument count.
    - Hard coded checks (``let`` for e.g.).
      NOTE: ideally there would be no hard coded checks, remove wherever possible.

    Without this, the LISP will be correct but not formatted in a way users might expect.
    '''
    use_native = cfg.style.use_native

    # Optional
    if node_parent.nodes_only_code and node_parent.brackets == '()':
        node = node_parent.nodes_only_code[0]
        if isinstance(node, NdSymbol):

            node_parent.index_wrap_hint = 1

            if node.data in {
                    'cl-letf',
                    'cl-letf*',
                    'if-let',
                    'if-let*',
                    'let',
                    'let*',
                    'pcase-let',
                    'pcase-let*',
                    'when-let',
                    'when-let*',
            }:
                # Only wrap with multiple declarations.
                if use_native:
                    if isinstance(node_parent.nodes_only_code[1], NdSexp):
                        if len(node_parent.nodes_only_code[1].nodes_only_code) > 1:
                            for subnode in node_parent.nodes_only_code[1].nodes_only_code[1:]:
                                subnode.force_newline = True
                else:
                    if isinstance(node_parent.nodes_only_code[1], NdSexp):
                        if len(node_parent.nodes_only_code[1].nodes_only_code) > 1:
                            for subnode in node_parent.nodes_only_code[1].nodes_only_code:
                                subnode.force_newline = True

                # A new line for each body of the let-statement.
                node_parent.index_wrap_hint = 2

                if not use_native:
                    if len(node_parent.nodes_only_code) > 2:
                        # While this should always be true, while editing it can be empty at times.
                        # Don't error in this case because it's annoying.
                        node_parent.nodes_only_code[2].force_newline = True

                node_parent.hints['indent'] = 1
                apply_relaxed_wrap(node_parent, cfg.style)
            elif node.data == 'cond':
                for subnode in node_parent.nodes_only_code[1:]:
                    subnode.force_newline = True
                    if isinstance(subnode, NdSexp) and len(subnode.nodes_only_code) >= 2:
                        subnode.nodes_only_code[1].force_newline = True
                        apply_relaxed_wrap_when_multiple_args(subnode, cfg.style)
            else:
                # First lookup built-in definitions, if they exist.
                if (fn_data := cfg.defs.fn_arity.get(node.data)) is not None:
                    # May be `FnArity` or a list.
                    symbol_type, nargs_min, nargs_max, hints = fn_data
                    if nargs_min is None:
                        nargs_min = 0

                    if hints is not None:
                        node_parent.hints.update(hints)
                    hints = node_parent.hints

                    hint_indent = hints.get('indent')
                    if hint_indent is not None:
                        # node_parent.index_wrap_hint = 1 + hint_indent
                        if symbol_type in {'special', 'macro'}:
                            if 'break' not in hints:
                                hints['break'] = 'always'

                    # First symbol counts for 1, another since wrapping takes place after this argument.
                    node_parent.index_wrap_hint = nargs_min + 1

                    # Wrap the first argument, instead of the last argument
                    # so all arguments are at an equal level as having the last
                    # argument split from the rest doesn't signify an important difference.
                    if symbol_type == 'func':
                        if hint_indent is None:
                            if hints is not None and hints.get('break_point') != 'overflow':
                                if node_parent.index_wrap_hint >= len(node_parent.nodes_only_code):
                                    node_parent.index_wrap_hint = 1

                    elif symbol_type == 'macro':
                        if nargs_max == 'many':
                            # So (with ...) macros don't keep the first argument aligned.
                            node_parent.wrap_all_or_nothing_hint = True
                            hints['break_point'] = 'overflow'

                    elif symbol_type == 'special':
                        # Used for special forms `unwind-protect`, `progn` .. etc.
                        node_parent.wrap_all_or_nothing_hint = True
                        if hints is None:
                            hints = {}
                        if 'break' not in hints:
                            hints['break'] = 'always'

                    if hints:
                        # Always wrap the doc-string.
                        if (hint_docstring := hints.get('doc-string')) is not None:
                            # NOTE: no support for evaluating EMACS-lisp from Python
                            # (so no support for symbol types).
                            if isinstance(hint_docstring, int):
                                node_parent.index_wrap_hint = min(node_parent.index_wrap_hint, hint_docstring)

                        if (hint_group := hints.get('group')) is not None:
                            assert isinstance(hint_group, list) and len(hint_group) == 2
                            group_beg, group_len = hint_group
                            assert isinstance(group_beg, int)
                            assert isinstance(group_len, int)
                            if len(node_parent.nodes_only_code) > group_beg + group_len + 1:
                                node_parent.index_wrap_hint = group_beg + group_len + 1
                            else:
                                # Group not in use.
                                del hints['group']

                        if (val := hints.get('break')) is not None:
                            if val == 'always':
                                apply_relaxed_wrap(node_parent, cfg.style)
                            elif val == 'multi':
                                apply_relaxed_wrap_when_multiple_args(node_parent, cfg.style)
                            elif val == 'to_wrap':  # Default
                                pass
                            else:
                                raise FmtException((
                                    'unknown "break" for {:s}, expected a value in '
                                    '["always", "multi", "to_wrap"]'
                                ).format(node.data))
                else:
                    if LOG_MISSING_DEFS is not None:
                        with open(LOG_MISSING_DEFS, 'a', encoding='utf-8') as fh:
                            fh.write('Missing: {:s}\n'.format(node.data))

    for node in node_parent.nodes_only_code:
        if isinstance(node, NdSexp):
            apply_rules(cfg, node)
        if not use_native:
            if node.force_newline:
                node_parent.force_newline = True

    if not use_native:
        node_parent.flush_newlines_from_nodes()


# ------------------------------------------------------------------------------
# Classes

# Immutable configuration,
# created from command line arguments.


class FmtStyle(NamedTuple):
    '''
    Details relating to formatting style.
    '''
    use_native: bool


class FmtConfig(NamedTuple):
    '''
    Configuration options relating to how the file should be formatted.
    '''
    style: FmtStyle
    use_trailing_parens: bool
    use_multiprocessing: bool
    fill_column: int
    empty_lines: int
    defs: FmtDefs


class FnArity(NamedTuple):
    '''
    Data associated with a function.
    '''
    # Type in: [`macro`, `func`, `special`].
    symbol_type: str
    # Minimum number of arguments.
    nargs_min: int
    # Maximum number of arguments, or strings: `many`, `unevalled`.
    nargs_max: Union[int, str]
    # Optional additional hints.
    hints: Optional[HintType]


class FmtDefs:
    '''
    Function definition data, hints about when to wrap arguments.
    '''
    __slots__ = (
        'fn_arity',
    )

    def __init__(
            self,
            *,
            # The key is the function name.
            fn_arity: Dict[str, FnArity],
    ):
        self.fn_arity = fn_arity

    def copy(self) -> FmtDefs:
        '''
        Return a copy of ``self``.
        '''
        return FmtDefs(fn_arity=self.fn_arity.copy())

    def prune_unused(self, fn_used: Set[str]) -> None:
        '''
        Remove unused identifiers using a ``fn_used`` set.
        '''
        fn_arity = self.fn_arity
        self.fn_arity = {k: v for k, v in fn_arity.items() if k in fn_used}
        fn_arity.clear()

    def from_json_files(self, fmt_defs: Iterable[str]) -> None:
        '''
        Load definitions from JSON files.
        '''
        import json
        for filepath in fmt_defs:
            with open(filepath, 'r', encoding='utf-8') as fh:
                try:
                    fh_as_json = json.load(fh)
                except Exception as ex:  # pylint: disable=W0703
                    sys.stderr.write('JSON definition: error ({:s}) parsing {!r}!\n'.format(str(ex), filepath))
                    continue

                functions_from_json = fh_as_json.get('functions')
                if functions_from_json is None:
                    continue

                if type(functions_from_json) is not dict:
                    sys.stderr.write(
                        'JSON definition: "functions" entry is a {!r}, expected a dict in {!r}!\n'.format(
                            type(functions_from_json).__name__,
                            filepath,
                        )
                    )
                    continue

                self.fn_arity.update(functions_from_json)


class FmtWriteCtx:
    '''
    Track context while writing.
    '''
    __slots__ = (
        'last_node',
        'is_newline',
        'line',
        'line_terminate',
        'cfg',
    )

    last_node: Optional[Node]
    is_newline: bool
    line: int
    line_terminate: int
    cfg: FmtConfig

    def __init__(self, cfg: FmtConfig):
        self.last_node = None
        self.is_newline = True
        self.line = 0
        self.line_terminate = -1
        self.cfg = cfg


class Node:
    '''
    Base class for all kinds of Lisp elements.
    '''
    __slots__ = (
        'force_newline',
        'original_line',
    )

    force_newline: bool
    original_line: int

    def calc_force_newline(self, style: FmtStyle) -> None:
        '''
        Function which must be overridden.
        '''
        raise Exception('All subclasses must define this')

    def __repr__(self) -> str:
        return 'force_newline={:d} line={:d}'.format(
            self.force_newline,
            self.original_line,
        )

    def fmt(
            self,
            ctx: FmtWriteCtx,
            write_fn: Callable[[str], Any],
            level: int,
            *,
            test: bool = False,
    ) -> None:
        '''
        Format function which must be overridden.
        '''
        raise Exception('All subclasses must define this')


# This is not enabled by default, hack for debugging only.
if USE_DEBUG_TRACE_NEWLINES:

    def _function_id(num_frames_up: int) -> str:
        '''
        Create a string naming the function n frames up on the stack.
        '''
        co = sys._getframe(num_frames_up + 1).f_code
        return '{:d} {:s}'.format(co.co_firstlineno, co.co_name)

    _Node = Node
    Node.__slots__ = tuple(s for s in Node.__slots__ if s != 'force_newline')  # type: ignore
    del Node

    class NodeTraceLines(_Node):
        '''
        Base class for all kinds of Lisp elements.
        '''
        __slots__ = (
            '_force_newline',
            '_force_newline_tracepoint',
        )

        original_line: int

        @property
        def force_newline(self) -> bool:
            '''
            Wrapper for ``_force_newline`` internal property.
            '''
            return self._force_newline

        @force_newline.setter
        def force_newline(self, value: bool) -> None:
            assert (value is True or value is False)
            if getattr(self, '_force_newline', None) != value:
                if value:
                    self._force_newline_tracepoint = (
                        _function_id(1) + ' ' +
                        _function_id(2)
                    )
            self._force_newline = value

    Node = NodeTraceLines  # type: ignore


class NdSexp(Node):
    '''
    Represents S-expressions (lists with curved or square brackets).
    '''
    __slots__ = (
        'prefix',
        'brackets',
        'nodes',
        'nodes_only_code',
        'index_wrap_hint',
        'wrap_all_or_nothing_hint',
        'hints',
        'prior_states',
        'fmt_cache',
    )

    def __init__(self, line: int, brackets: str, nodes: Optional[List[Node]] = None):
        self.original_line = line
        self.prefix: str = ''
        self.brackets = brackets
        self.nodes = nodes or []
        self.index_wrap_hint: int = 1
        self.wrap_all_or_nothing_hint: bool = False
        self.hints: HintType = {}
        self.prior_states: List[NdSexp_WrapState] = []
        self.fmt_cache = ''

    def __repr__(self) -> str:
        import textwrap
        return '{:s}({:s} prefix=({:s})\n{:s})'.format(
            self.__class__.__name__,
            Node.__repr__(self),
            self.prefix,
            '\n'.join(textwrap.indent(repr(node), '  ') for node in self.nodes),
        )

    def is_multiline(self) -> bool:
        '''
        Return true if this S-expression spans multiple lines.
        '''
        for node in self.iter_nodes_recursive():
            if node.force_newline:
                return True
        return False

    def count_recursive(self) -> int:
        '''
        Return the number of elements within this node (recursively), excluding it's self.
        '''
        count = len(self.nodes)
        for node in self.nodes_only_code:
            if isinstance(node, NdSexp):
                count += node.count_recursive()
        return count

    def node_last_for_trailing_parens_test(self) -> Optional[Node]:
        '''
        Return the node which would have trialing parenthesis written after it or None if it's not a code node
        since a trailing comment for e.g. will never have parenthesis written directly after it.
        '''
        if self.nodes and self.nodes_only_code:
            if self.nodes[-1] is self.nodes_only_code[-1]:
                return self.nodes[-1]
        return None

    def iter_nodes_recursive(self) -> Generator[Node, None, None]:
        '''
        Iterate over all nodes recursively.
        '''
        for node in self.nodes:
            yield node
            if isinstance(node, NdSexp):
                yield from node.iter_nodes_recursive()

    def iter_nodes_recursive_with_self(self) -> Generator[Node, None, None]:
        '''
        Iterate over all nodes recursively, including this node (first).
        '''
        yield self
        for node in self.nodes:
            yield node
            if isinstance(node, NdSexp):
                yield from node.iter_nodes_recursive()

    def iter_nodes_recursive_with_parent(self) -> Generator[Tuple[Node, NdSexp], None, None]:
        '''
        Iterate over all nodes recursively, with the parent node as well.
        '''
        for node in self.nodes:
            yield (node, self)
            if isinstance(node, NdSexp):
                yield from node.iter_nodes_recursive_with_parent()

    def iter_nodes_recursive_with_prior_state(self, visited: Set[int]) -> Generator[NdSexp, None, None]:
        '''
        Specialized iterator for looping over nodes that have a ``prior_state`` set.

        This takes a ``visited`` argument to prevent over-iteration, so this can be called multiple times
        on different levels of the S-expression tree, without re-looping over data a large number of times.
        '''
        for node in self.nodes:
            if isinstance(node, NdSexp):
                visited_len = len(visited)
                visited.add(id(node))
                if visited_len != len(visited):
                    if node.prior_states:
                        yield node
                    yield from node.iter_nodes_recursive_with_prior_state(visited)

    def iter_nodes_recursive_with_prior_state_and_self(self, visited: Set[int]) -> Generator[NdSexp, None, None]:
        '''
        A version of ``iter_nodes_recursive_with_prior_state`` that includes ``self`` (last).
        '''
        yield from self.iter_nodes_recursive_with_prior_state(visited)
        visited_len = len(visited)
        visited.add(id(self))
        if visited_len != len(visited):
            if self.prior_states:
                yield self

    def newline_state_get(self) -> NdSexp_WrapState:
        '''
        Return the wrapped state of this S-expressions nodes.
        '''
        return tuple(node.force_newline for node in self.nodes)

    def newline_state_set(self, state: NdSexp_WrapState) -> None:
        '''
        Set the wrapped state of this S-expressions nodes.
        '''
        for data, node in zip(state, self.nodes):
            node.force_newline = data

    def calc_nodes_level_next(self, cfg: FmtConfig, level: int) -> Sequence[int]:
        '''
        Return a ``self.nodes`` aligned list of next levels.
        The list may be shorter, in this case the last element should be used
        for node indices that exceed this lists range.
        '''
        if not cfg.style.use_native or cfg.fill_column == 0:
            if level == -1:
                return [0]
            return [level + 2]

        # The complex 'native' case.
        node_code_index_pre_newline = 0
        if self.hints:
            indent = indent_orig = self.hints.get('indent')
            if type(indent) is str:
                fn_data_test = cfg.defs.fn_arity.get(indent)
                if fn_data_test is not None:
                    hints_test = fn_data_test[3]
                    if hints_test is not None:
                        indent = hints_test.get('indent')
                        if not isinstance(indent, int):
                            # Unlikely, avoid unexpected cases.
                            indent = None
                del fn_data_test

            if indent is not None:
                if isinstance(indent, int):
                    hint_docstring = self.hints.get('doc-string')
                    if hint_docstring is not None:
                        # TODO: no support for evaluating EMACS-lisp from Python.
                        if isinstance(hint_docstring, int):
                            indent = min(hint_docstring - 1, indent)
                    del hint_docstring
                else:
                    indent = None
        else:
            indent_orig = None
            indent = None

        if level == -1:
            level_next_first = 0
            level_next_pre = 0
            level_next_post = 0
        else:
            level_next_base = level + len(self.prefix)
            level_next_first = level_next_base + 1
            level_next_pre = level_next_base + 1
            level_next_post = level_next_base + 1

            if (len(self.nodes_only_code) > 1) and isinstance(self.nodes_only_code[0], NdSymbol):
                if self.nodes_only_code[0].force_newline is False:
                    if self.nodes_only_code[1].force_newline is False:
                        # Add 1 for the space for the trailing space.
                        # Values may be overwritten below.
                        level_next_first = level_next_pre = level_next_base + len(self.nodes_only_code[0].data) + 2
                        node_code_index_pre_newline = 1

            if indent is not None:
                if isinstance(self.nodes_only_code[0], NdSymbol):
                    fn_data = cfg.defs.fn_arity.get(self.nodes_only_code[0].data)
                else:
                    fn_data = None
                if fn_data is not None:
                    symbol_type, nargs_min, nargs_max, _hints = fn_data

                # Perhaps this should be indented further.
                if (
                        len(self.nodes_only_code) > 1 and
                        isinstance(self.nodes_only_code[0], NdSymbol) and
                        self.nodes_only_code[0].force_newline is False
                ):

                    two_or_more_non_wrapped_args = True
                    for node in self.nodes_only_code[1:3]:
                        if node.force_newline:
                            two_or_more_non_wrapped_args = False
                            break

                    if self.nodes_only_code[1].force_newline:
                        is_aligned = False
                    elif fn_data and symbol_type == 'special':
                        # This is used for e.g.
                        #    (condition-case err
                        #        (progn
                        #          test
                        #          case)
                        #      (error case))
                        is_aligned = False
                    elif (
                            fn_data is not None and
                            (nargs_min <= indent) and
                            two_or_more_non_wrapped_args and
                            # fancy-compilation needs this.
                            # WARNING: this is odd but for e.g.
                            #    (defun foo
                            #        (long arg list)
                            #      "Doc string.")
                            # is what emacs does. so follow this.
                            self.nodes_only_code[0].data != 'defun'
                    ):
                        # This is used for e.g.
                        #
                        #    (example foo bar
                        #             test)
                        is_aligned = True
                    else:
                        # This is used for e.g.
                        #    (when
                        #        (progn
                        #          a
                        #          b)
                        #      c)
                        is_aligned = False

                    if is_aligned:
                        level_next_pre = level_next_base + len(self.nodes_only_code[0].data) + 2
                    else:
                        level_next_pre = level_next_base + 4

                    level_next_post = level_next_base + 2

                    if fn_data is not None:
                        # All wrapped, no special indents handling.
                        if indent_orig == 'defun':
                            if (
                                    (nargs_max == 'unevalled' and symbol_type == 'special') or
                                    symbol_type in {'func', 'macro'}
                            ):
                                level_next_pre = level_next_base + 2
                                level_next_post = level_next_base + 2

        node_code_index = 0
        node_code_index_next = 0
        if indent is not None:
            assert isinstance(indent, int)
            node_code_index_to_indent = indent
        else:
            # Never use 'level_next_post', when no indent, everything is 'pre'.
            node_code_index_to_indent = len(self.nodes_only_code) + 1

        level_next_data = []
        for node in self.nodes:
            node_code_index = node_code_index_next
            if node_code_index <= node_code_index_pre_newline:
                level_next_data.append(level_next_first)
            elif node_code_index <= node_code_index_to_indent:
                level_next_data.append(level_next_pre)
            else:
                level_next_data.append(level_next_post)
                # All future elements will use this value.
                break

            if isinstance(node, NODE_CODE_TYPES):
                node_code_index_next = node_code_index + 1
            else:
                node_code_index_next = node_code_index

        return level_next_data

    def flush_newlines_from_nodes(self) -> bool:
        '''
        Ensure parent nodes are on their own-line when any of their children are multi-line.
        '''
        # `assert not cfg.use_native` (if we had `cfg`).
        changed = False
        if not self.force_newline:
            for node in self.nodes_only_code:
                if node.force_newline:
                    self.force_newline = True
                    changed = True
                    break
        return changed

    def flush_newlines_from_nodes_for_native(self) -> bool:
        '''
        Ensure some kinds of expressions are wrapped onto new files.
        '''
        # Ensure There is never trailing non-wrapped S-expressions: e.g:
        #
        #    (a b c d (e
        #              f
        #              g))
        #
        # This is only permissible for the first or second arguments, e.g:
        #
        #    (a (e
        #        f
        #        g))
        #
        # While this could be supported currently it's not and I feel this adds awkward right shift.
        # `assert cfg.use_native` # If we have `cfg`.
        changed = False
        for i, node in enumerate(self.nodes_only_code):
            if i > 1 and isinstance(node, NdSexp) and not node.force_newline and node.is_multiline():
                node.force_newline = True
                changed = True
                # if not self.force_newline:
                #     self.force_newline = True
        return changed

    def flush_newlines_from_nodes_for_native_recursive(self) -> bool:
        changed = False
        for i, node in enumerate(self.nodes_only_code):
            if i > 1 and isinstance(node, NdSexp) and not node.force_newline and node.is_multiline():
                node.force_newline = True
                changed = True
                # if not self.force_newline:
                #     self.force_newline = True
            if isinstance(node, NdSexp):
                changed |= node.flush_newlines_from_nodes_for_native_recursive()
        return changed

    def flush_newlines_from_nodes_recursive(self) -> bool:
        '''
        Flush new-lines from children to parents.
        '''
        # `assert not cfg.use_native` # If we have `cfg`.
        changed = False
        for node in self.nodes_only_code:
            if node.force_newline:
                if not self.force_newline:
                    changed = True
                    self.force_newline = True
            if isinstance(node, NdSexp):
                changed |= node.flush_newlines_from_nodes_recursive()
        return changed

    def calc_force_newline(self, style: FmtStyle) -> None:
        force_newline = False
        node_prev = None
        for node in self.nodes:
            node.calc_force_newline(style)
            if not node.force_newline:
                if node_prev:
                    if isinstance(node_prev, NdComment):
                        node.force_newline = True
                    elif isinstance(node_prev, NdSymbol):
                        # Always keep trailing back-slashes,
                        # these are used to define a string literal across multiple lines.
                        if node_prev.data == '\\' and (node_prev.original_line != node.original_line):
                            node.force_newline = True
            force_newline |= node.force_newline
            node_prev = node

        if not style.use_native:
            self.force_newline = force_newline
        else:
            self.force_newline = False

    def finalize_parse(self) -> None:
        '''
        Perform final operations after parsing.
        '''
        # Connect: ' (  to '(
        i = len(self.nodes) - 1
        while i > 0:
            node = self.nodes[i]
            if isinstance(node, NdSexp):
                node_prev = self.nodes[i - 1]
                if isinstance(node_prev, NdSymbol):
                    if (
                            not node_prev.data.strip('#,`\'') or
                            # Some macros use `#foo(a(b(c)))` which need to be connected.
                            is_hash_prefix_special_case(node_prev.data)
                    ):
                        del self.nodes[i - 1]
                        node.prefix = node_prev.data
                        i -= 1
            i -= 1

        self.nodes_only_code: List[Node] = [
            node for node in self.nodes
            if isinstance(node, NODE_CODE_TYPES)
        ]
        for node in self.nodes_only_code:
            if isinstance(node, NdSexp):
                node.finalize_parse()

    def finalize_style(self, cfg: FmtConfig) -> None:
        '''
        Perform final operations after parsing.
        '''
        # Strip blank lines at the start or end of S-expressions.
        for i in (-1, 0):
            while self.nodes and isinstance(self.nodes[i], NdWs):
                del self.nodes[i]

        # Apply maximum blank lines.
        i = len(self.nodes) - 1

        count = 0
        while i > 1:
            node = self.nodes[i]
            if isinstance(node, NdWs):
                if count == cfg.empty_lines:
                    del self.nodes[i]
                else:
                    count += 1
            else:
                count = 0
            i -= 1
        del count

        for node in self.nodes_only_code:
            if isinstance(node, NdSexp):
                node.finalize_style(cfg)

    def fmt_check_exceeds_colum_max(
            self,
            cfg: FmtConfig,
            level: int,
            trailing_parens: int,
            *,
            calc_score: bool,
            test_node_terminate: Optional[Node] = None,
    ) -> int:
        '''
        :arg calc_score: When true, the return value is a score.
        '''
        if cfg.fill_column == 0:
            raise Exception('internal error, this should not be called')

        # Simple optimization, don't calculate excess white-space.
        fill_column = cfg.fill_column - level
        level = 0
        _ctx = FmtWriteCtx(cfg)

        # Avoid writing the string:
        # Either calculate a score or early exit with an exception on the first over-length line found.
        # This block can be removed without causing any problems, it just avoids some excessive work.
        if test_node_terminate is None:

            line_length = 0

            if calc_score:
                # Accumulate a score.
                score = 0

                def write_fn_fast(text: str) -> None:
                    nonlocal line_length
                    nonlocal score
                    i = text.find('\n')
                    if i == -1:
                        line_length += len(text)
                        if line_length > fill_column:
                            score += 2 ** (line_length - fill_column)
                    else:
                        i_prev = 0
                        while True:
                            if i == -1:
                                line_length += len(text) - i_prev
                                if line_length > fill_column:
                                    score += 2 ** (line_length - fill_column)
                                break

                            line_length += i - i_prev
                            if line_length > fill_column:
                                score += 2 ** (line_length - fill_column)

                            i_prev = i + 1
                            line_length = 0
                            i = text.find('\n', i + 1)

                self.fmt_with_terminate_node(_ctx, write_fn_fast, level, test=True)
                line_length += trailing_parens
                if line_length > fill_column:
                    score += 2 ** (line_length - fill_column)

                return score
            else:
                # Simple, detect if the line length is exceeded.

                def write_fn_fast(text: str) -> None:
                    nonlocal line_length
                    i = text.find('\n')
                    if i == -1:
                        line_length += len(text)
                        if line_length > fill_column:
                            raise FmtExceptionEarlyExit
                    else:
                        i_prev = 0
                        while True:
                            if i == -1:
                                line_length += len(text) - i_prev
                                if line_length > fill_column:
                                    raise FmtExceptionEarlyExit
                                break

                            line_length += i - i_prev
                            if line_length > fill_column:
                                raise FmtExceptionEarlyExit

                            i_prev = i + 1
                            line_length = 0
                            i = text.find('\n', i + 1)

                try:
                    self.fmt_with_terminate_node(_ctx, write_fn_fast, level, test=True)
                except FmtExceptionEarlyExit:
                    return 1
                if line_length + trailing_parens > fill_column:
                    return 1
                return 0

        _data: List[str] = []
        write_fn = _data.append

        self.fmt_with_terminate_node(_ctx, write_fn, level, test=True, test_node_terminate=test_node_terminate)

        line_terminate = -1
        if not cfg.use_trailing_parens:
            if _ctx.line_terminate == _ctx.line:
                line_terminate = _ctx.line_terminate
            elif test_node_terminate is None:
                line_terminate = _ctx.line

        data = ''.join(_data)

        if calc_score:
            return calc_over_long_line_score(data, fill_column, trailing_parens, line_terminate)
        return calc_over_long_line_length_test(data, fill_column, trailing_parens, line_terminate)

    def fmt(self,
            ctx: FmtWriteCtx,
            write_fn: Callable[[str], Any],
            level: int,
            *,
            test: bool = False,
            ) -> None:
        '''
        Write this node to a file.
        '''
        if self.fmt_cache:
            write_fn(self.fmt_cache)
            return
        self.fmt_with_terminate_node(ctx, write_fn, level, test=test)

    def fmt_with_terminate_node(
            self,
            ctx: FmtWriteCtx,
            write_fn: Callable[[str], Any],
            level: int,
            *,
            test: bool = False,
            test_node_terminate: Optional[Node] = None,
    ) -> None:
        '''
        Write this node to a file with support for terminating early.
        '''

        line_sexpr_prev = ctx.line

        if level != -1:
            if ctx.is_newline:
                write_fn(' ' * level)

            if self.prefix:
                write_fn(self.prefix)

                if ctx.cfg.style.use_native:
                    pass
                else:
                    # A single `#` is used for advanced macros,
                    # we can't put a new-line after them.
                    if (not is_hash_prefix_special_case(self.prefix)) and self.is_multiline():
                        write_fn('\n')

                        if USE_DEBUG_TRACE_NEWLINES:
                            if self.force_newline:
                                if not test:
                                    write_fn(' $' + self._force_newline_tracepoint)  # type: ignore

                        ctx.line += 1
                        ctx.is_newline = True
                        write_fn(' ' * level)
                        ctx.is_newline = False

            write_fn(self.brackets[0])
            ctx.is_newline = False

        ctx.last_node = self

        is_first = True

        node_prev_is_multiline = False

        level_next_data = self.calc_nodes_level_next(ctx.cfg, level)
        level_next_data_last = len(level_next_data) - 1
        for i, node in enumerate(self.nodes):
            level_next = level_next_data[min(i, level_next_data_last)]

            if test:
                # Use only for testing.
                if node is test_node_terminate:
                    # We could return however this misses trailing parenthesis on the same line.
                    assert ctx.line_terminate == -1
                    ctx.line_terminate = ctx.line

            if (
                    node.force_newline or
                    (
                        node_prev_is_multiline and
                        # Don't push trailing comments onto new line.
                        not (isinstance(node, NdComment) and node.is_own_line is False)
                    )
            ):
                if not ctx.is_newline:
                    write_fn('\n')
                    ctx.line += 1
                    ctx.is_newline = True

            if isinstance(node, NdWs):
                pass
            else:
                if ctx.is_newline:

                    if USE_DEBUG_TRACE_NEWLINES:
                        if node.force_newline:
                            if not test:
                                write_fn(' $' + node._force_newline_tracepoint)  # type: ignore

                    write_fn(' ' * level_next)

                    ctx.is_newline = False
                else:
                    if (
                            (not is_first) or

                            # Ensure we have:
                            #     ( ;; Comment.
                            #
                            # Instead of:
                            #     (;; Comment.
                            (isinstance(node, NdComment) and node.is_own_line is False)
                    ):
                        write_fn(' ')

            if ctx.line_terminate not in (-1, ctx.line):
                return

            line = ctx.line

            node.fmt(ctx, write_fn, level_next, test=test)
            node_prev_is_multiline = (line != ctx.line)

            ctx.last_node = node

            is_first = False

            if ctx.line_terminate not in (-1, ctx.line):
                return

        if level != -1:
            if ctx.is_newline:
                write_fn(' ' * level_next)
                ctx.is_newline = False
            else:
                if (
                        (ctx.cfg.use_trailing_parens and ctx.line != line_sexpr_prev) or
                        isinstance(ctx.last_node, NdComment)
                ):
                    write_fn('\n')
                    ctx.line += 1
                    ctx.is_newline = True
                    write_fn(' ' * level_next)
                    ctx.is_newline = False
            write_fn(self.brackets[1])
            ctx.is_newline = False


# ------------------------------------------------------------------------------
# Formatting Solver

def fmt_solver_fill_column_wrap_relaxed(
        cfg: FmtConfig,
        node_parent: NdSexp,
        level: int,
        trailing_parens: int) -> None:
    '''
    Perform relaxed wrapping for blocks where any lines exceed the fill-column.
    '''
    # First be relaxed, then again if it fails.
    if node_parent.fmt_check_exceeds_colum_max(cfg, level, trailing_parens, calc_score=False):

        if cfg.fill_column != 0:
            if not node_parent.wrap_all_or_nothing_hint:
                state_init = node_parent.newline_state_get()

        apply_relaxed_wrap(node_parent, cfg.style)

        if cfg.fill_column != 0:
            if not node_parent.wrap_all_or_nothing_hint:
                state_test = node_parent.newline_state_get()
                if state_init != state_test:
                    node_parent.prior_states.append(state_init)

        if not cfg.style.use_native:
            node_parent.force_newline = True


def fmt_solver_fill_column_wrap_each_argument(
        cfg: FmtConfig,
        node_parent: NdSexp,
        level: int,
        trailing_parens: int) -> None:
    '''
    Check that each argument fits within the fill-column,
    wrapping as necessary.
    Note that this uses much more involved checks than ``fmt_solver_fill_column_wrap_relaxed``.
    '''
    assert node_parent.index_wrap_hint != 0
    assert len(node_parent.nodes_only_code) > 1

    node_force_newline = []
    # Wrap items before if absolutely needed, one at a time.
    force_newline = False
    i_last = len(node_parent.nodes_only_code) - 1
    i = min(node_parent.index_wrap_hint, i_last)
    assert i > 0

    node = node_parent.nodes_only_code[i]
    score_init = node_parent.fmt_check_exceeds_colum_max(
        cfg,
        level,
        trailing_parens,
        calc_score=True,
        test_node_terminate=node,
    )

    if score_init:
        # Don't attempt the wrap the first item,
        # as this will simply push it onto the line below.
        #
        # This:
        #   (argument "Long string that does not fit")
        #
        # Gets converted to this:
        #   (
        #     argument
        #     "Long string that does not fit")
        #
        # Where as we would prefer this:
        #   (argument
        #     "Long string that does not fit")
        #
        while i != 0:
            node = node_parent.nodes_only_code[i]
            if not node.force_newline:
                node.force_newline = True
                node_force_newline.append(node)
                force_newline = True

                if not node_parent.fmt_check_exceeds_colum_max(
                    cfg,
                    level,
                    trailing_parens,
                    calc_score=False,
                    test_node_terminate=node,
                ):
                    # Imply 'node_parent.wrap_all_or_nothing_hint', even when not set.
                    hints = node_parent.hints
                    if hints.get('break_point') == 'overflow':
                        pass
                    else:
                        # TODO: warn about unknown break_point.

                        # When the break was added before `node_parent.index_wrap_hint`,
                        # wrap everything so there are no breaks added in random locations
                        # that might seems significant.
                        if i < node_parent.index_wrap_hint:
                            for j in range(1, i):
                                node_iter = node_parent.nodes_only_code[j]
                                if not node_iter.force_newline:
                                    node_iter.force_newline = True
                                    node_force_newline.append(node_iter)
                    break
            i -= 1

        i = min(node_parent.index_wrap_hint, i_last)
        node = node_parent.nodes_only_code[i]
        score_test = node_parent.fmt_check_exceeds_colum_max(
            cfg,
            level,
            trailing_parens,
            calc_score=True,
            test_node_terminate=node,
        )

        # If none of the changes made an improvement, revert them.
        if score_init <= score_test:
            for node_iter in node_force_newline:
                node_iter.force_newline = False
            force_newline = False

    if not cfg.style.use_native:
        if force_newline:
            node_parent.force_newline = True

    # If after wrapping 'everything', we still overflow,
    # don't use  this for tests in future, it confuses checks
    # causing other lines to wrap because of this node.
    if cfg.style.use_native:
        node = node_parent.nodes_only_code[1]
        if not node.force_newline:
            score_init = node_parent.fmt_check_exceeds_colum_max(
                cfg,
                level,
                trailing_parens,
                calc_score=True,
            )
            if score_init:
                node.force_newline = True
                score_test = node_parent.fmt_check_exceeds_colum_max(
                    cfg,
                    level,
                    trailing_parens,
                    calc_score=True,
                )
                if score_test < score_init:
                    # Success, don't exclude.
                    pass
                else:
                    # Don't add line break.
                    node.force_newline = False


def fmt_solver_fill_column_wrap(cfg: FmtConfig, node_parent: NdSexp, level: int, trailing_parens: int) -> None:
    '''
    For lists that will need wrapping even when all parents are wrapped,
    wrap these beforehand.
    '''
    if not node_parent.nodes_only_code:
        return

    node_parent_is_multiline_prev = node_parent.is_multiline()

    node_trailing_parens = node_parent.node_last_for_trailing_parens_test()

    level_next_data = node_parent.calc_nodes_level_next(cfg, level)
    level_next_data_last = len(level_next_data) - 1
    for i, node in enumerate(node_parent.nodes):
        if isinstance(node, NdSexp):
            level_next = level_next_data[min(i, level_next_data_last)]
            fmt_solver_fill_column_wrap(
                cfg,
                node,
                level_next,
                trailing_parens + 1 if node is node_trailing_parens else 0,
            )
            if not cfg.style.use_native:
                if node.force_newline:
                    node_parent.force_newline = True

    if cfg.fill_column != 0:
        state_init = node_parent.newline_state_get()

        if len(node_parent.nodes_only_code) > 1:
            fmt_solver_fill_column_wrap_relaxed(cfg, node_parent, level, trailing_parens)
            fmt_solver_fill_column_wrap_each_argument(cfg, node_parent, level, trailing_parens)

    # Some blocks don't allow mixed wrapping.
    if node_parent.wrap_all_or_nothing_hint:
        if node_parent_is_multiline_prev or node_parent.is_multiline():
            apply_relaxed_wrap(node_parent, cfg.style)

    if cfg.fill_column != 0:
        state_test = node_parent.newline_state_get()
        if state_init != state_test:
            node_parent.prior_states.append(state_init)


def fmt_solver_fill_column_unwrap(
        cfg: FmtConfig,
        node_parent: NdSexp,
        level: int,
        trailing_parens: int,
        visited: Set[int],
) -> None:
    '''
    Wrap lines that were split back onto the same line.
    In some cases this is useful because:

    - A nested S-expression is wrapped to fit.
    - On of the S-expressions containing it is later wrapped onto multiple lines (de-indenting in some cases).
    - There is no longer a need for the original nested S-expression to be wrapped.

    In this case it makes sense to set the wrapping to previously known valid states.
    Note that testing this is quite computationally expensive, so add additional checks with care.
    '''
    if not node_parent.nodes_only_code:
        return

    node_trailing_parens = node_parent.node_last_for_trailing_parens_test()

    level_next_data = node_parent.calc_nodes_level_next(cfg, level)
    level_next_data_last = len(level_next_data) - 1
    for i, node in enumerate(node_parent.nodes):
        if isinstance(node, NdSexp):
            level_next = level_next_data[min(i, level_next_data_last)]
            fmt_solver_fill_column_unwrap(
                cfg,
                node,
                level_next,
                trailing_parens + 1 if node is node_trailing_parens else 0,
                visited,
            )
            if not cfg.style.use_native:
                if node.force_newline:
                    node_parent.force_newline = True

    # If this isn't a newline, let a parent node handle it
    # otherwise leading nodes won't be included.
    parent_score_curr = -1
    calc_score = True
    state_curr: NdSexp_WrapState = ()
    if node_parent.force_newline:
        if level == 0:
            # When at level zero, include ourself,
            # needed unless this function is called with a single `root` node.
            nodes_with_prior_state = node_parent.iter_nodes_recursive_with_prior_state_and_self
        else:
            nodes_with_prior_state = node_parent.iter_nodes_recursive_with_prior_state

        for node in nodes_with_prior_state(visited):
            if parent_score_curr == -1:
                parent_score_curr = node_parent.fmt_check_exceeds_colum_max(
                    cfg,
                    level,
                    trailing_parens,
                    calc_score=True,
                )
                # If the current state has no over-length lines (such as long comments).
                # There is no need to do extra work. Any over-long line caused by the state being
                # tested can immediately be considered an error and early exit.
                # In this case the score will only ever be 0/1 but that's fine.
                if parent_score_curr == 0:
                    calc_score = False

            # While in general duplicates states are not added,
            # it's also not guaranteed that this can never happen. And it in-fact does sometimes.
            # Since it's fairly rare, track states here which have already been tested.
            #
            # This avoids the need to de-duplicate when adding, and means if the first unwrap is successful
            # then there is no need to track visited states at all.
            state_curr = node.newline_state_get()
            state_visit = {state_curr}

            for state_test in node.prior_states:

                if state_test in state_visit:
                    continue

                node.newline_state_set(state_test)

                if fmt_solver_newline_constraints_apply(node, cfg):
                    state_test = node.newline_state_get()
                    if state_test in state_visit:
                        node.newline_state_set(state_curr)
                        continue

                parent_score_test = node_parent.fmt_check_exceeds_colum_max(
                    cfg,
                    level,
                    trailing_parens,
                    calc_score=calc_score,
                )
                if parent_score_test <= parent_score_curr:
                    parent_score_curr = parent_score_test
                    state_curr = state_test
                    # The most ambitious (early) states are first, no need to try others.
                    break

                # This state was unsuccessful, don't attempt to test it again.
                state_visit.add(state_test)

                node.newline_state_set(state_curr)
            # Avoid checking these ever again - either they were useful or not.
            node.prior_states.clear()


def fmt_solver_newline_constraints_apply(node_parent: NdSexp, cfg: FmtConfig) -> bool:
    '''
    Add newlines based on constraints (untreated to the fill-column).
    Return true when a change was made.
    '''
    use_native = cfg.style.use_native

    changed = False

    # # From `fmt_solver_fill_column_wrap`, technically correct but not needed.
    # if node_parent.wrap_all_or_nothing_hint and node_parent.is_multiline():
    #     apply_relaxed_wrap(node_parent, cfg.style)

    # Finally, if the node is multi-line, ensure it's also split at the hinted location.
    # Ensures we don't get:
    #     (or foo
    #        (bar
    #          bob))
    #
    # Instead it's all one line:
    #     (or foo (bar bob))
    #
    # Or both are wrapped onto a new line:
    #     (or
    #       foo
    #       (bar bob))
    #
    # ... respecting the hint for where to split.
    #
    if node_parent.is_multiline():
        if len(node_parent.nodes_only_code) > node_parent.index_wrap_hint:
            node = node_parent.nodes_only_code[node_parent.index_wrap_hint]
            if not node.force_newline:
                node.force_newline = True
                changed = True

        # Ensure colon prefixed arguments are on new-lines
        # if the block is multi-line.
        #
        # When multi-line, don't do:
        #     (foo
        #       :keyword long-value-which-causes-next-line-to-wrap
        #       :other value :third value)
        #
        # Instead do:
        #     (foo
        #       :keyword long-value-which-causes-next-line-to-wrap
        #       :other value
        #       :third value)
        # But don't do:
        #     (:eval
        #        ...)

        # node_prev = None
        for node in node_parent.nodes_only_code[node_parent.index_wrap_hint:]:
            if not node.force_newline:
                if (
                        isinstance(node, NdSymbol) and
                        node.data.startswith(':') and
                        (node is not node_parent.nodes_only_code[0])
                ):
                    # if (
                    #         isinstance(node_prev_prev, NdSymbol) and
                    #         node_prev_prev.data.startswith(':')
                    # ):
                    if not node.force_newline:
                        node.force_newline = True
                        changed = True

            # node_prev = node

    if use_native:
        changed |= node_parent.flush_newlines_from_nodes_for_native()

    return changed


def fmt_solver_newline_constraints_apply_recursive(node_parent: NdSexp, cfg: FmtConfig) -> None:
    '''
    Perform line wrapping, taking indent-levels into account.
    '''
    # First handle S-expressions one at a time, then all of them.
    # not very efficient, but it avoids over wrapping.

    force_newline = False

    for i, node in enumerate(node_parent.nodes):
        if isinstance(node, NdSexp):
            fmt_solver_newline_constraints_apply_recursive(node, cfg)
        force_newline |= node.force_newline

    if cfg.style.use_native:
        pass
    else:
        if force_newline:
            node_parent.force_newline = True

    if fmt_solver_newline_constraints_apply(node_parent, cfg):
        if not cfg.style.use_native:
            node_parent.force_newline = True


def fmt_solver_for_root_node(cfg_base: FmtConfig, node: NdSexp) -> None:
    '''
    Calculate line wrapping for top-level nodes.
    '''
    apply_rules(cfg_base, node)

    # This purpose of using two passes is as follows:
    # - Perform all formatting without applying a fill-column.
    # - Then wrap storing the `prior_states` which may be restored again when unwrapping.
    #
    # Using two passes ensures that we only ever restore into configurations
    # that would be valid if the fill column allows for them to fit.
    # Without this, it would be possible to restore into states that are not valid,
    # or at least would not exist after all the logic for wrapping lines was applied.
    for pass_index in (0, 1):
        if pass_index == 0:
            cfg_args = cfg_base._asdict()
            cfg_args['fill_column'] = 0
            cfg = FmtConfig(**cfg_args)
        else:
            cfg = cfg_base

            if USE_PARANOID_ASSERT:
                # No stages should be added on the initial pass.
                for n in node.iter_nodes_recursive_with_self():
                    if isinstance(n, NdSexp):
                        assert bool(n.prior_states) is False

        fmt_solver_fill_column_wrap(cfg, node, 0, 0)

        fmt_solver_newline_constraints_apply_recursive(node, cfg)

        if pass_index != 0:
            if cfg.style.use_native:
                fmt_solver_fill_column_unwrap(cfg_base, node, 0, 0, set())
            if USE_PARANOID_ASSERT:
                if node.flush_newlines_from_nodes_for_native_recursive():
                    raise Exception('this should be maintained while unwrapping!')


def fmt_solver_for_root_node_multiprocessing(cfg: FmtConfig, node_group: Sequence[NdSexp]) -> Sequence[str]:
    '''
    A version of ``fmt_solver_for_root_node`` which supports multi-processing.
    '''
    result_group = []
    ctx = FmtWriteCtx(cfg)
    for node in node_group:
        fmt_solver_for_root_node(cfg, node)
        data: List[str] = []
        node.fmt(ctx, data.append, 0)
        result_group.append(''.join(data))
        del data
    return result_group


# ------------------------------------------------------------------------------
# Formatting Utilities

# Currently this always represents a blank line.
class NdWs(Node):
    '''
    This represents white-space to be kept in the output.
    '''
    __slots__ = ()

    def __init__(self, line: int):
        self.original_line = line

    def __repr__(self) -> str:
        return '{:s}({:s} type=blank_line)'.format(
            self.__class__.__name__,
            Node.__repr__(self),
        )

    def calc_force_newline(self, style: FmtStyle) -> None:
        # False because this forces it's own newline
        self.force_newline = True

    def fmt(
            self,
            ctx: FmtWriteCtx,
            write_fn: Callable[[str], Any],
            level: int,
            *,
            test: bool = False,
    ) -> None:
        write_fn('\n')
        ctx.line += 1
        ctx.is_newline = True


class NdComment(Node):
    '''
    Code-comment.
    '''
    __slots__ = (
        'data',
        'is_own_line',
    )

    data: str
    is_own_line: bool

    def __init__(self, line: int, data: str, is_own_line: bool):
        self.original_line = line
        self.data = data
        self.is_own_line = is_own_line

    def __repr__(self) -> str:
        return '{:s}({:s} data=\'{:s}\')'.format(
            self.__class__.__name__,
            Node.__repr__(self),
            self.data,
        )

    def calc_force_newline(self, style: FmtStyle) -> None:
        self.force_newline = self.is_own_line

    def fmt(
            self,
            ctx: FmtWriteCtx,
            write_fn: Callable[[str], Any],
            level: int,
            *,
            test: bool = False,
    ) -> None:
        write_fn(';')
        write_fn(self.data)
        ctx.is_newline = False


class NdString(Node):
    '''
    A string literal.
    '''
    __slots__ = (
        'data',
        'lines',
    )

    data: str
    lines: int

    def __init__(self, line: int, data: str):
        self.original_line = line
        self.data = data
        self.lines = self.data.count('\n')

    def __repr__(self) -> str:
        return '{:s}({:s} \'{:s}\')'.format(
            self.__class__.__name__,
            Node.__repr__(self),
            self.data,
        )

    def calc_force_newline(self, style: FmtStyle) -> None:
        if USE_WRAP_LINES:
            self.force_newline = ((not self.data.startswith('\n')) and self.lines > 0)
        else:
            self.force_newline = False

    def fmt(
            self,
            ctx: FmtWriteCtx,
            write_fn: Callable[[str], Any],
            level: int,
            *,
            test: bool = False,
    ) -> None:
        write_fn('"')
        write_fn(self.data)
        write_fn('"')
        ctx.is_newline = False
        ctx.line += self.lines


class NdSymbol(Node):
    '''
    This represents any identifier that isn't an S-expression, string, comment or white-space.
    '''
    __slots__ = (
        'data',
    )

    data: str

    def __init__(self, line: int, data: str):
        self.original_line = line
        self.data = data

    def __repr__(self) -> str:
        return '{:s}({:s} data=\'{:s}\')'.format(
            self.__class__.__name__,
            Node.__repr__(self),
            self.data,
        )

    def calc_force_newline(self, style: FmtStyle) -> None:
        self.force_newline = False

    def fmt(
            self,
            ctx: FmtWriteCtx,
            write_fn: Callable[[str], Any],
            level: int,
            *,
            test: bool = False,
    ) -> None:
        write_fn(self.data)
        ctx.is_newline = False


NODE_CODE_TYPES = (NdSymbol, NdString, NdSexp)


# ------------------------------------------------------------------------------
# File Parsing

def parse_file(fh: TextIO) -> Tuple[str, NdSexp]:
    '''
    Parse the file ``fh``, returning:
    - The first un-parsed line (for ELISP files starting with a bang (``#!``)).
    - The ``NdSexp`` (root node of the S-expression tree).
    '''
    from io import StringIO

    line = 0

    # Fake top level S-expression to populate, (brackets aren't used).
    root = NdSexp(line, brackets='()')

    # Current S-expressions.
    sexp_ctx = [root]
    sexp_level = 0

    line_has_contents = False

    # Special case, a lisp file with a shebang.
    first_line_unparsed = ''
    c_peek: Optional[str] = fh.read(1)
    if c_peek == '#':
        first_line_chars = [c_peek]
        c_peek = None
        while c := fh.read(1):
            first_line_chars.append(c)
            if c == '\n':
                line += 1
                break
        first_line_unparsed = ''.join(first_line_chars)
        del first_line_chars

    while c := c_peek or fh.read(1):
        c_peek = None
        # NOTE: Can use 'match c' here, will bump minimum Python version to 3.10
        if c == '(':  # Open S-expression.
            sexp_ctx.append(NdSexp(line, '()'))
            sexp_ctx[sexp_level].nodes.append(sexp_ctx[-1])
            sexp_level += 1
            line_has_contents = True

        elif c == '[':  # Open vector.
            sexp_ctx.append(NdSexp(line, '[]'))
            sexp_ctx[sexp_level].nodes.append(sexp_ctx[-1])
            sexp_level += 1
            line_has_contents = True
        elif c == ')':  # Close S-expression.
            if sexp_level == 0:
                raise FmtException('additional closing brackets, line {}'.format(line))
            if sexp_ctx.pop().brackets[0] != '(':
                raise FmtException(
                    'closing bracket "{:s}" line {:d}, unmatched bracket types, expected ")"'.format(c, line)
                )
            sexp_level -= 1
            line_has_contents = True
        elif c == ']':  # Close vector.
            if sexp_level == 0:
                raise FmtException('additional closing brackets, line {}'.format(line))
            if sexp_ctx.pop().brackets[0] != '[':
                raise FmtException(
                    'closing bracket "{:s}" line {:d}, unmatched bracket types, expected "]"'.format(c, line)
                )
            sexp_level -= 1
            line_has_contents = True
        elif c == '"':  # Open & close string.
            data = StringIO()
            is_slash = False
            while (c := fh.read(1)):
                if c == '"' and not is_slash:
                    break
                data.write(c)
                if c == '\\':
                    is_slash = not is_slash
                else:
                    is_slash = False
                    if c == '\n':
                        line += 1

            if not c:
                raise FmtException('parsing string at line {}'.format(line))

            sexp_ctx[sexp_level].nodes.append(NdString(line, data.getvalue()))
            del data, is_slash, c
            line_has_contents = True
        elif c == ';':  # Comment.
            data = StringIO()
            while (c_peek := fh.read(1)) not in {'', '\n'}:
                c = c_peek
                c_peek = None
                data.write(c)

            is_own_line = not line_has_contents
            sexp_ctx[sexp_level].nodes.append(NdComment(line, data.getvalue(), is_own_line))
            del data, is_own_line
            line_has_contents = True
        elif c == '\n':  # White-space (newline).
            line += 1
            # Respect blank lines up until the limit.
            if line_has_contents is False:
                sexp_ctx[sexp_level].nodes.append(NdWs(line))
            line_has_contents = False
        elif c in {' ', '\t'}:  # White-space (space, tab) - ignored.
            pass
        else:  # Symbol (any other character).
            data = StringIO()
            is_slash = False
            while c:
                if c == '\\':
                    is_slash = not is_slash
                else:
                    is_slash = False
                data.write(c)
                c_peek = fh.read(1)
                if not c_peek:
                    break
                if c_peek == '\n':
                    break
                if not is_slash:
                    if c_peek in {
                            '(', ')',
                            '[', ']',
                            ';',
                            ' ', '\t',
                            # Lisp doesn't require spaces are between symbols and quotes.
                            '"',
                    }:
                        break

                c = c_peek
                c_peek = None

            text = data.getvalue()
            del data

            # Special support for character literals.
            if text[0] == '?':
                if c_peek:
                    # Always include the next character
                    # even if it's normally a delimiting character such as ';', '"'
                    # (un-escaped literal support, even allowing for `?;` or `?\C-;`).
                    if (
                            # Support `? ` and `?;`.
                            (len(text) == 1) or
                            # Support `?\C- ` and `?\C-;` and `?\C-\s- `.
                            (len(text) >= 4 and (
                                text[-1] == '-' and
                                text[-2].isalpha() and
                                text[-3] == '\\')
                             )
                    ):
                        text = text + c_peek
                        c_peek = None

            sexp_ctx[sexp_level].nodes.append(NdSymbol(line, text))
            del is_slash
            line_has_contents = True

    if sexp_level != 0:
        raise FmtException('unbalanced S-expressions at file-end, found {} levels, expected 0'.format(sexp_level))

    root.finalize_parse()

    return first_line_unparsed, root


def write_file(cfg: FmtConfig, fh: TextIO, root: NdSexp, first_line: str) -> None:
    '''
    Write the ``root`` S-expression into ``fh``.
    '''

    ctx = FmtWriteCtx(cfg)

    if first_line:
        fh.write(first_line)
    root.fmt(ctx, fh.write, -1)
    fh.write('\n')


def node_group_by_count(root: NdSexp, *, chunk_size_limit: int) -> Sequence[Sequence[NdSexp]]:
    '''
    Return top-level nodes from ``root``, grouped by ``chunk_size_limit``.
    '''
    assert chunk_size_limit > 0

    if chunk_size_limit == 1:
        return [
            [node] for node in root.nodes_only_code
            if isinstance(node, NdSexp)
        ]

    chunk_size_curr = 0
    node_group_list: List[List[NdSexp]] = [[]]
    for node in root.nodes_only_code:
        if isinstance(node, NdSexp):
            count_recursive = node.count_recursive()
            if count_recursive >= chunk_size_limit:
                # This block can be it's own group, keep the current one at the end.
                group = node_group_list[-1]
                node_group_list[-1] = [node]
                node_group_list.append(group)

            elif chunk_size_curr >= chunk_size_limit:
                chunk_size_curr = count_recursive
                node_group_list.append([node])
            else:
                chunk_size_curr += count_recursive
                node_group_list[-1].append(node)

    # It's possible all blocks where over the size limit and an empty group remains at the end.
    if len(node_group_list[-1]) == 0:
        node_group_list.pop()

    return node_group_list


def do_wrap_level_0_multiprocessing(cfg: FmtConfig, root: NdSexp, parallel_jobs: int) -> None:
    '''
    A version of ``do_wrap_level_0`` which uses multi-processing.
    '''
    node_group_list = node_group_by_count(root, chunk_size_limit=256)

    args = [(cfg, node_group) for node_group in node_group_list]

    import multiprocessing

    if parallel_jobs == 0:
        parallel_jobs = multiprocessing.cpu_count()

    with multiprocessing.Pool(processes=parallel_jobs) as pool:
        result_group_list = pool.starmap(fmt_solver_for_root_node_multiprocessing, args)
        for node_group, result_group in zip(node_group_list, result_group_list):
            for node, result in zip(node_group, result_group):
                node.fmt_cache = result


def do_wrap_level_0(cfg: FmtConfig, root: NdSexp) -> None:
    '''
    Calculate wrapping for all nodes.
    '''
    for node in root.nodes_only_code:
        if isinstance(node, NdSexp):
            fmt_solver_for_root_node(cfg, node)


def format_file(
        filepath: str,
        cfg: FmtConfig,
        *,
        parallel_jobs: int = 0,
        use_stdin: bool = False,
        use_stdout: bool = False,
) -> None:
    '''
    Main file formatting function.
    '''

    # Needed as files may contain '\r' only, see emacs own:
    # `lisp/cedet/semantic/grammar-wy.el`
    newline = '\r\n' if (os.name == 'nt') else '\n'

    if use_stdin:
        first_line, root = parse_file(sys.stdin)
    else:
        with open(filepath, 'r', encoding='utf-8', newline=newline) as fh:
            first_line, root = parse_file(fh)

    if USE_EXTRACT_DEFS:
        parse_local_defs(cfg.defs, root)

    # Has newline at file start?
    # it will disable the settings such as lexical binding.
    # The intention of re-formatting is not to make any functional changes,
    # so it's important to add the blank line back.
    stars_with_bank_line = False
    if root.nodes:
        if isinstance(root.nodes[0], NdWs):
            stars_with_bank_line = True

    root.finalize_style(cfg)

    if stars_with_bank_line:
        # Add back the blank line.
        root.nodes.insert(0, NdWs(0))

    root.calc_force_newline(cfg.style)

    apply_comment_force_newline(root)

    # Redundant but needed for the assertion not to fail in the case when `len(root.nodes_only_code) == 1`.
    root.force_newline = True

    if USE_WRAP_LINES:

        # All root level nodes get their own line always.
        for node in root.nodes_only_code:
            node.force_newline = True

        if cfg.use_multiprocessing:
            # Copying this information can be quite slow, prune unused items first.
            fn_used: Set[str] = set()
            scan_used_fn_defs(cfg.defs, root, fn_used)
            cfg.defs.prune_unused(fn_used)

            do_wrap_level_0_multiprocessing(cfg, root, parallel_jobs)
        else:
            do_wrap_level_0(cfg, root)

        if USE_PARANOID_ASSERT:
            for node in root.iter_nodes_recursive():
                if isinstance(node, NdSexp):
                    assert bool(node.prior_states) is False

    if cfg.style.use_native:
        pass
    else:
        if not cfg.use_multiprocessing:
            assert root.flush_newlines_from_nodes_recursive() is False

    if use_stdout:
        write_file(cfg, sys.stdout, root, first_line)
    else:
        with open(filepath, 'w', encoding='utf-8', newline=newline) as fh:
            write_file(cfg, fh, root, first_line)


# ------------------------------------------------------------------------------
# Argument Parsing


def argparse_create() -> argparse.ArgumentParser:
    '''
    Create the argument parser used to format from the command line.
    '''

    # When `--help` or no arguments are given, print this help.
    usage_text = 'Format emacs-lisp.'

    epilog = (
        'This program formats emacs lisp, from the standard input, '
        'or operating on files, in-place.'
    )

    parser = argparse.ArgumentParser(description=usage_text, epilog=epilog)

    parser.add_argument(
        '--fmt-defs-dir',
        dest='fmt_defs_dir',
        metavar='DIR',
        default='',
        type=str,
        required=False,
        help='Directory used for storing definitions.',
    )
    parser.add_argument(
        '--fmt-defs',
        dest='fmt_defs',
        metavar='FILES',
        default='',
        type=str,
        required=False,
        help=(
            'Definition filenames within "--fmt-defs-dir" when only a filename is specified, '
            'otherwise absolute paths are used. '
            'split by PATH_SEPARATOR. '
            '(internal use, this is written by Emacs).'
        ),
    )
    parser.add_argument(
        '--fmt-style',
        dest='fmt_style',
        default='native',
        help=(
            'Formatting style in where "native" mimics EMACS default indentation and "fixed" '
            'formats using a simple 2-space for each nested block rule.'
        ),
        required=False,
        choices=('native', 'fixed')
    )

    parser.add_argument(
        '--quiet',
        dest='use_quiet',
        default=False,
        action='store_true',
        required=False,
        help='Don\t output any status messages.',
    )

    parser.add_argument(
        '--fmt-trailing-parens',
        dest='fmt_use_trailing_parens',
        default=False,
        action='store_true',
        required=False,
        help='Give each trailing parenthesis it\'s own line.',
    )

    parser.add_argument(
        '--fmt-fill-column',
        dest='fmt_fill_column',
        default=99,
        nargs='?',
        type=int,
        required=False,
        help='Maxumum column width.',
    )

    parser.add_argument(
        '--fmt-empty-lines',
        dest='fmt_empty_lines',
        default=2,
        nargs='?',
        type=int,
        required=False,
        help='Maximum column width.',
    )

    parser.add_argument(
        '--parallel-jobs',
        dest='parallel_jobs',
        default=0,
        nargs='?',
        type=int,
        required=False,
        help='The number of parallel processes to use (zero to select automatically).',
    )

    parser.add_argument(
        '--stdin',
        dest='use_stdin',
        default=False,
        action='store_true',
        required=False,
        help='Use the stdin for file contents instead of the file name passed in.',
    )

    parser.add_argument(
        '--stdout',
        dest='use_stdout',
        default=False,
        action='store_true',
        required=False,
        help='Use the stdout to output the file contents instead of the file name passed in.',
    )

    parser.add_argument(
        '--exit-code',
        dest='exit_code',
        default=0,
        nargs='?',
        type=int,
        required=False,
        help='Exit code to use upon successfully re-formatting',
    )

    parser.add_argument(
        'files',
        nargs=argparse.REMAINDER,
        help='All trailing arguments are treated as file paths to format.'
    )
    return parser


# ------------------------------------------------------------------------------
# Main Function

def main_generate_defs() -> bool:
    '''
    A utility to generate definitions from a file without loading it.
    Needed when definitions are requested but the code may not be loaded into emacs.

    As it's not expected that a formatting tool would executed arbitrary (untrusted)
    LISP code. It's necessary to extract definitions of untrusted code so that formatting
    can be correctly performed.
    '''
    try:
        i = sys.argv.index('--gen-defs')
    except ValueError:
        return False

    args_rest = sys.argv[i + 1:]

    while args_rest:
        file_input = args_rest.pop(0)
        file_output = args_rest.pop(0)

        defs = FmtDefs(fn_arity={})

        with open(file_input, 'r', encoding='utf-8') as fh:
            _, root = parse_file(fh)

            parse_local_defs(defs, root)

        with open(file_output, 'w', encoding='utf-8') as fh:
            fh.write('{\n')
            fh.write('"functions": {\n')
            is_first = False
            for key, val in defs.fn_arity.items():
                if is_first:
                    fh.write(',\n')
                # Generated hints are always empty.
                symbol_type, nargs_min, nargs_max, _hints = val
                nargs_min_str = str(nargs_min) if isinstance(nargs_min, int) else '"{:s}"'.format(nargs_min)
                nargs_max_str = str(nargs_max) if isinstance(nargs_max, int) else '"{:s}"'.format(nargs_max)
                fh.write('"{:s}": ["{:s}", {:s}, {:s}, {{}}]'.format(
                    key, symbol_type, nargs_min_str, nargs_max_str,
                ))
                is_first = True
            fh.write('')
            fh.write('}\n')  # 'functions'.
            fh.write('}\n')

    return True


def main() -> None:
    '''
    The main function which handles problems parsing the document gracefully.
    Other kinds of errors are not expected so will show a typical (less user friendly) trace-back.
    '''
    if main_generate_defs():
        return

    args = argparse_create().parse_args()

    if args.use_stdin and args.use_stdout:
        if args.files:
            sys.stderr.write(
                'The \'--files\' argument cannot be used when both stdin and stdout are used {!r}\n'.format(args.files))
            sys.exit(1)
    elif args.use_stdin or args.use_stdout:
        if len(args.files) != 1:
            sys.stderr.write(
                'The \'--stdin\' & \'--stdout\' arguments are limited to a single file {!r}\n'.format(args.files))
            sys.exit(1)
    elif not args.files:
        sys.stderr.write(
            'No files passed in, pass in files or use both \'--stdin\' & \'--stdout\'\n')
        sys.exit(1)

    defs_orig = FmtDefs(fn_arity={})

    if args.fmt_defs:
        defs_orig.from_json_files(
            os.path.join(args.fmt_defs_dir, filename) if (os.sep not in filename) else filename
            for filename in args.fmt_defs.split(os.pathsep)
        )

    count_files_error = 0
    count_files_total = 0

    for i, filepath in enumerate(args.files or ('',)):

        # Make a copy of the original definition if this is one of many files.
        if i == 0:
            defs = defs_orig if len(args.files) <= 1 else defs_orig.copy()
        elif i + 1 < len(args.files):
            defs = defs_orig.copy()
        else:
            defs = defs_orig  # Last iteration, no need to copy.

        # Use the `stderr` to avoid conflicting with `stdout` when it's set.
        if (not args.use_quiet) and (not args.use_stdout):
            sys.stdout.write('{:s}\n'.format(filepath))

        cfg = FmtConfig(
            style=FmtStyle(
                use_native=args.fmt_style == 'native',
            ),
            use_trailing_parens=args.fmt_use_trailing_parens,
            use_multiprocessing=args.parallel_jobs >= 0,
            fill_column=args.fmt_fill_column,
            empty_lines=args.fmt_empty_lines,
            defs=defs,
        )

        try:
            format_file(
                filepath,
                cfg=cfg,
                parallel_jobs=args.parallel_jobs,
                use_stdin=args.use_stdin,
                use_stdout=args.use_stdout,
            )
        except FmtException as ex:
            if filepath:
                sys.stderr.write('Error: {:s} in {:s}\n'.format(str(ex), filepath))
            else:
                sys.stderr.write('Error: {:s}\n'.format(str(ex)))
            count_files_error += 1

        count_files_total += 1

    if count_files_error:
        if count_files_total > 1:
            sys.stderr.write('Error: {:d} of {:d} files failed to format!\n'.format(
                count_files_error,
                count_files_total,
            ))
        sys.exit(1)

    sys.exit(args.exit_code)


if __name__ == '__main__':
    main()
