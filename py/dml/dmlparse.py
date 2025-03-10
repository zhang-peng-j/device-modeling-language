# © 2021-2022 Intel Corporation
# SPDX-License-Identifier: MPL-2.0

# Parser for DML 1.2

import os, sys, re
from ply import lex, yacc

from .logging import *
from .messages import *
from . import ast, logging
import dml.globals
from . import dmllex12
from . import dmllex14

assert lex.__version__ == yacc.__version__ == "3.4"

class UnexpectedEOF(Exception): pass

    # Priorities are as described in Harbison & Steele, "C - A Reference
    # Manual", 5th ed., Section 7.2, although using multiples of 10,
    # with C++-operators such as 'new','delete' and 'throw' inserted
    # with the corresponding priorities (cf. Stroustrup, Section 6.2).
    #
    # operator                  priority        associativity
    # -------------------------------------------------------
    # a[k] f(...) . -> i++ i--  160             Left
    # ++i --i                   150             Right
    # sizeof                    150             Right
    # ~ ! -i +i & *p            150             Right
    # new delete                150             Right
    # (type)                    140             Right
    # * / %                     130             Left
    # + -                       120             Left
    # << >>                     110             Left
    # < <= > >=                 100             Left
    # == !=                      90             Left
    # &                          80             Left
    # ^                          70             Left
    # |                          60             Left
    # &&                         50             Left
    # ||                         40             Left
    # ?:                         30             Right
    # = += -= *= /= %=           20             Right
    # <<= >>= &= ^= |=           20             Right
    # ,                          10

def precedence(dml_version):
    return (
    ('nonassoc', 'LOWEST_PREC'),
    ('nonassoc', 'ELSE') + (() if dml_version == (1, 2) else ('HASHELSE',)),
    ('left', 'LOG'),
    ('left', 'THROW'),
    ('left', 'CASE'),
    ('right', 'EQUALS', 'PLUSEQUAL', 'MINUSEQUAL', 'TIMESEQUAL',
     'DIVEQUAL', 'MODEQUAL', 'BOREQUAL', 'BANDEQUAL', 'BXOREQUAL',
     'RSHIFTEQUAL', 'LSHIFTEQUAL'),
    ('right', 'COLON') + (() if dml_version == (1, 2) else ('HASHCOLON',)),
    ('right', 'CONDOP') + (() if dml_version == (1, 2) else ('HASHCONDOP',)),
    ('left', 'LOR'),
    ('left', 'LAND'),
    ('left', 'BOR'),
    ('left', 'BXOR'),
    ('left', 'BAND'),
    ('left', 'EQ', 'NE'),
    ('left', 'LT', 'GT', 'LE', 'GE'),
    ('left', 'LSHIFT', 'RSHIFT'),
    ('left', 'PLUS', 'MINUS'),
    ('left', 'TIMES', 'DIVIDE', 'MOD'),
    ('right', 'CAST'),
    ('right', 'NEW', 'DELETE', 'BNOT', 'LNOT', 'SIZEOF',
     'PLUSPLUS', 'MINUSMINUS', 'DEFINED', 'TYPEOF',
     'unary_prefix') + (('HASH',) if dml_version == (1, 2) else ()),
    ('left', 'CALL', 'INLINE', ),
    ('left', 'PERIOD', 'ARROW', 'LBRACKET', 'LPAREN', 'unary_postfix')
    )

# This stores the location of the current AST node
def default_site(t, elt=1):
    lexpos = t.lexpos(elt)
    # This assertion would trigger if the parser suppressed position
    # tracking, which would sometimes happen if tracking=False was
    # passed to parse().
    assert lexpos != 0 or t.lineno(elt) != 0
    return DumpableSite(t.parser.file_info, lexpos)

site = default_site

lexspan_map = {}
def extended_site(t, elt=1):
    s = default_site(t, elt)
    (start, _) = t.lexspan(1)
    (_, end) = t.lexspan(-1)
    lexspan_map[s] = (start, end)
    return s

def track_lexspan():
    '''Make the parser save additional position information. This is
    slightly expensive and seldom useful, but it's used by some
    porting messages. Currently only enabled with the -P flag.

    When enabled, the positions of the first and last token of each
    grammar rule can be retrieved by lex_start() and lex_end().

    Note that currently nothing will be tracked for cached ASTs,
    i.e. when loading from a .dmlast file. This is not a problem in
    practice for -P, since only files from the standard library are cached.
    '''
    global site
    site = extended_site
def start_site(site):
    while isinstance(site, TemplateSite):
        site = site.site
    assert lexspan_map
    if site not in lexspan_map:
        return None
    (start, _) = lexspan_map[site]
    return DumpableSite(site.file_info, start)
def end_site(site):
    while isinstance(site, TemplateSite):
        site = site.site
    assert lexspan_map
    if site not in lexspan_map:
        # lexspan_map is uninitialized when an AST is loaded from
        # .dmlast. Luckily, this is not a problem in practice, since
        # lexspan_map is only used for porting messages, and .dmlast
        # is only created for files that nobody wants to auto-port.
        # We still produce a well-formed site, to avoid confusing
        # port-dml's tag parser
        if os.path.isfile(site.filename() + 'ast'):
            return SimpleSite(site.filename() + ':1:1')
        # unknown...
        return None
    (_, end) = lexspan_map[site]
    return DumpableSite(site.file_info, end)

def lex_end_site(t, elt):
    info = t.parser.file_info
    (start, end) = t.lexspan(elt)
    return DumpableSite(info, end)

def parse_bitorder(t, syn):
    if syn not in ('be', 'le'):
        report(EBITO(site(t), syn))
        return 'le'
    return syn

# Map function name to production rule function
production_rules_dml12 = {}
production_rules_dml14 = {}

def prod_dml12(f):
    '''Decorator for functions that should be used as production rules
    in the DML 1.2 grammar'''
    name = 'p_' + f.__name__
    assert name not in production_rules_dml12, f.__name__
    production_rules_dml12[name] = f
    return f

def prod_dml14(f):
    '''Decorator for functions that should be used as production rules
    in the DML 1.4 grammar'''
    name = 'p_' + f.__name__
    assert name not in production_rules_dml14, f.__name__
    production_rules_dml14[name] = f
    return f

def prod(f):
    '''Decorator for functions that should be used as production rules
    in all DML grammars'''
    return prod_dml12(prod_dml14(f))

# Normal entry point for device files

@prod
def device(t):
    'dml : DEVICE objident SEMI maybe_bitorder device_statements'
    t[0] = ast.dml(site(t), t[2], t[5])

# Entry point for imported files

@prod
def import_file(t):
    'dml : maybe_bitorder device_statements'
    t[0] = ast.dml(site(t), None, t[2])

@prod
def maybe_bitorder_no(t):
    'maybe_bitorder : '
    t.parser.file_info.bitorder = 'le'

@prod
def maybe_bitorder_yes(t):
    'maybe_bitorder : BITORDER ident SEMI'
    t.parser.file_info.bitorder = parse_bitorder(t, t[2])

@prod
def device_statements(t):
    'device_statements : device_statements device_statement'
    # collect a list
    t[0] = t[1] + [t[2]]

@prod
def device_statements_empty(t):
    'device_statements : '
    t[0] = []

@prod_dml12
def device_statement(t):
    '''device_statement : object_statement
                        | toplevel'''
    t[0] = t[1]

# like object_statement, but also permits top-level constructs, and in
# addition, #if bodies also allow top-level constructs
@prod_dml14
def device_statement(t):
    '''device_statement : toplevel
                        | object
                        | parameter
                        | method
                        | bad_shared_method
                        | istemplate SEMI
                        | toplevel_if
                        | error_stmt
                        | in_each'''
    if t[1].kind == 'typedparam':
        report(ESYNTAX(site(t), ':',
                       'parameter type declaration only permitted'
                       + ' in top level template block'))
        # fallback: dummy statement
        t[0] = ast.hashif(site(t), ast.variable(site(t), 'false'), [], [])
    else:
        t[0] = t[1]

allowed_in_hashif = {
    'object',
    'saved',
    'session',
    'method',
    'hashif',
    'error',
}

@prod_dml14
def toplevel_if(t):
    '''toplevel_if : hashif LPAREN expression RPAREN \
                   LBRACE device_statements RBRACE toplevel_else'''
    if all(stmt.kind in allowed_in_hashif for stmt in t[6] + t[8]):
        t[0] = ast.hashif(site(t), t[3], t[6], t[8])
    else:
        report(WEXPERIMENTAL(site(t), "'if' body with top-level statements"))
        t[0] = ast.toplevel_if(site(t), t[3], t[6], t[8])

@prod_dml14
def toplevel_else_no(t):
    '''toplevel_else :'''
    t[0] = []

@prod_dml14
def toplevel_else_body(t):
    '''toplevel_else : hashelse LBRACE device_statements RBRACE'''
    t[0] = t[3]

@prod_dml14
def toplevel_else_if(t):
    '''toplevel_else : hashelse toplevel_if'''
    t[0] = [t[2]]

# Objects

@prod_dml12
def object_anonymous_bank(t):
    'object : BANK object_spec'
    t[0] = ast.object_(site(t), None, 'bank', [], t[2])

@prod
def array_list_empty(t):
    'array_list : '
    t[0] = []

@prod
def array_list(t):
    'array_list : array_list LBRACKET arraydef RBRACKET'
    t[0] = t[1] + [t[3]]

@prod
def object_regarray(t):
    'object : REGISTER objident array_list sizespec offsetspec maybe_istemplate object_spec'
    t[0] = ast.object_(site(t), t[2], 'register', t[3],
                       t[4] + t[5] + t[6] + t[7])

@prod
def bitrangespec(t):
    'bitrangespec : AT bitrange'
    t[0] = t[2]

@prod
def bitrangespec_empty(t):
    'bitrangespec :'
    t[0] = []

@prod_dml14
def object_field(t):
    'object : FIELD objident array_list bitrangespec maybe_istemplate object_spec'
    t[0] = ast.object_(site(t), t[2], 'field', t[3], t[4] + t[5] + t[6])

def endian_translate_bit(expr, width, bitorder):
    if bitorder == 'be':
        s = expr.site
        return ast.binop(s, ast.binop(s, width, '-', ast.int(s, 1)),
                         '-', expr)
    else:
        return expr

def parent_bitsize(site):
    if site.dml_version() == (1, 2):
        return ast.member(site, ast.objectref(site, 'reg'), '.', 'bitsize')
    else:
        return ast.member(site, ast.variable(site, 'parent'), '.', 'bitsize')

# Bit range used in field declaration
@prod
def bitrange_1(t):
    'bitrange : LBRACKET expression RBRACKET'
    bit_expr = endian_translate_bit(t[2], parent_bitsize(t[2].site),
                                    t.parser.file_info.bitorder)
    t[0] = [ast.parameter(site(t), 'msb', (bit_expr, None, None)),
            ast.parameter(site(t), 'lsb', (bit_expr, None, None))]

@prod
def bitrange_2(t):
    'bitrange : LBRACKET expression COLON expression RBRACKET'
    bitorder = t.parser.file_info.bitorder
    t[0] = [
        ast.parameter(
            site(t), 'msb',
            (endian_translate_bit(t[2], parent_bitsize(t[2].site), bitorder),
             None, None)),
        ast.parameter(
            site(t), 'lsb',
            (endian_translate_bit(t[4], parent_bitsize(t[4].site), bitorder),
             None, None))]

@prod_dml12
def object_field_1(t):
    'object : FIELD objident bitrange maybe_istemplate object_spec'
    if logging.show_porting:
        report(PFIELDRANGE(site(t, 3)))
    t[0] = ast.object_(site(t), t[2], 'field', [], t[3] + t[4] + t[5])

@prod_dml12
def field_array_size_no(t):
    'fieldarraysize : '
    t[0] = []

@prod_dml12
def field_array_size(t):
    'fieldarraysize : LBRACKET ident IN expression DOTDOT expression RBRACKET fieldarraysize'
    if t[4].kind != 'int' or t[4].args != (0,):
        report(EZRANGE(site(t, 4)))
    s = site(t)
    t[0] = [(t[2], ast.binop(s, ast.int(s, 1), '+', t[6]))] + t[8]
    if logging.show_porting:
        # j in 0..expr => j < expr + 1
        # We allow this conversion to be less polished than the
        # standard PARRAY, because it's seldom used.
        remove_from = site(t, 7)
        new = ' + 1'
        report(PARRAY(site(t, 3), site(t, 5), remove_from,
                      end_site(t[6].site), new))

@prod_dml12
def object_field_2(t):
    'object : FIELD objident fieldarraysize bitrangespec maybe_istemplate object_spec'
    t[0] = ast.object_(site(t), t[2], 'field', t[3], t[4] + t[5] + t[6])

@prod_dml14
def data(t):
    'data : SESSION'
    t[0] = t[1]

@prod_dml12
def data(t):
    'data : DATA'
    if logging.show_porting:
        report(PSESSION(site(t), 'data', 'session'))
    t[0] = t[1]

@prod
def object_session(t):
    'object : session_decl'
    t[0] = t[1]

@prod
def session_decl(t):
    'session_decl : data named_cdecl SEMI'
    (_, _, name, typ) = t[2]
    t[0] = ast.session(site(t), name, typ, None)

@prod
def session_decl_init(t):
    'session_decl : data named_cdecl EQUALS initializer SEMI'
    (_, _, name, typ) = t[2]
    t[0] = ast.session(site(t), name, typ, t[4])

@prod_dml14
def object_saved(t):
    'object : saved_decl'
    t[0] = t[1]

@prod_dml14
def saved_decl(t):
    'saved_decl : SAVED named_cdecl SEMI'
    (_, _, name, typ) = t[2]
    t[0] = ast.saved(site(t), name, typ, None)

@prod_dml14
def saved_decl_init(t):
    'saved_decl : SAVED named_cdecl EQUALS initializer SEMI'
    (_, _, name, typ) = t[2]
    t[0] = ast.saved(site(t), name, typ, t[4])

@prod
def object3(t):
    '''object : CONNECT   objident array_list maybe_istemplate object_spec
              | INTERFACE objident array_list maybe_istemplate object_spec
              | ATTRIBUTE objident array_list maybe_istemplate object_spec
              | BANK      objident array_list maybe_istemplate object_spec
              | EVENT     objident array_list maybe_istemplate object_spec
              | GROUP     objident array_list maybe_istemplate object_spec
              | PORT      objident array_list maybe_istemplate object_spec
              | IMPLEMENT objident array_list maybe_istemplate object_spec'''
    t[0] = ast.object_(site(t), t[2], t[1], t[3], t[4] + t[5])

@prod_dml12
def maybe_extern_yes(t):
    '''maybe_extern : EXTERN'''
    t[0] = True

@prod_dml12
def maybe_extern_no(t):
    '''maybe_extern :'''
    t[0] = False

@prod
def maybe_default_yes(t):
    '''maybe_default : DEFAULT'''
    t[0] = True

@prod
def maybe_default_no(t):
    '''maybe_default :'''
    t[0] = False

@prod_dml12
def object_method_noinparams(t):
    '''method : METHOD maybe_extern objident method_outparams maybe_default compound_statement'''
    name = t[3]
    (inp, outp, throws) = ([], t[4], True)
    if logging.show_porting:
        i = min([4, 5, 6], key=lambda i: t.lexpos(i))
        report(PINPARAMLIST(site(t, i)))
    if logging.show_porting and outp:
        (start, end) = t.lexspan(6)
        [stmts] = t[6].args
        ends_with_return = stmts and stmts[-1].kind == 'return'
        (_, rparen) = t.lexspan(4)
        report(PRETVAL(
            site(t), DumpableSite(t.parser.file_info, start),
            DumpableSite(t.parser.file_info, end),
            DumpableSite(t.parser.file_info, rparen),
            [(psite.loc(), pname) for (_, psite, pname, _) in outp],
            ends_with_return))

    body = t[6]
    t[0] = ast.method(site(t), name, (inp, outp, throws, body),
                      t[5], t[2], lex_end_site(t, -1))

@prod_dml14
def object_method(t):
    '''method : METHOD objident method_params_typed maybe_default compound_statement'''
    name = t[2]
    (inp, outp, throws) = t[3]
    body = t[5]
    t[0] = ast.method(site(t), name, (inp, outp, throws, body),
                      t[4], False, lex_end_site(t, -1))

@prod_dml14
def object_inline_method(t):
    '''method : INLINE METHOD objident method_params_maybe_untyped maybe_default compound_statement'''
    name = t[3]
    (inp, outp, throws) = t[4]
    if all(typ for (_, asite, name, typ) in inp):
        # inline annotation would have no effect for fully typed methods.
        # We forbid it as a way to strongly discourage unneeded use of inline.
        report(ESYNTAX(site(t, 2), 'inline',
                       'only use inline if there are untyped arguments'))
    body = t[6]
    t[0] = ast.method(site(t), name, (inp, outp, throws, body),
                      t[5], False, lex_end_site(t, -1))

@prod_dml12
def object_method(t):
    '''method : METHOD maybe_extern objident LPAREN cdecl_or_ident_list RPAREN method_outparams maybe_nothrow maybe_default compound_statement'''
    name = t[3]
    inp = t[5]
    outp = t[7]
    throws = t[8]
    if logging.show_porting and any(not typ for (_, _, name, typ) in inp):
        # some standard methods are assigned a type later on
        if name not in ['set', 'write']:
            report(PINLINEDECL(site(t), 'method', 'inline method'))
        for (_, decl_site, argname, typ) in inp:
            if not typ:
                report(PINLINEDECL(decl_site, argname, 'inline ' + argname))
    if logging.show_porting and outp:
        (start, end) = t.lexspan(10)
        [stmts] = t[10].args
        ends_with_return = stmts and stmts[-1].kind == 'return'
        (_, rparen) = t.lexspan(7)
        report(PRETVAL(
            site(t), DumpableSite(t.parser.file_info, start),
            DumpableSite(t.parser.file_info, end),
            DumpableSite(t.parser.file_info, rparen),
            [(psite.loc(), pname) for (_, psite, pname, _) in outp],
            ends_with_return))

    body = t[10]
    t[0] = ast.method(site(t), name, (inp, outp, throws, body),
                      t[9], t[2], lex_end_site(t, -1))

@prod_dml12
def arraydef1(t):
    '''arraydef : expression'''
    t[0] = ('i', t[1])
    if logging.show_porting:
        report(PARRAY_I(site(t)))

@prod_dml12
def arraydef2(t):
    '''arraydef : ident IN expression DOTDOT expression'''
    if t[3].kind != 'int' or t[3].args != (0,):
        report(EZRANGE(site(t, 3)))
    s = site(t)
    t[0] = (t[1], ast.binop(s, ast.int(s, 1), '+', t[5]))
    if logging.show_porting:
        if t[5].kind == 'int':
            # j in 0..4 => j < 5
            (max_index,) = t[5].args
            remove_from = site(t, 5)
            new = str(max_index + 1)
        else:
            # j in 0..expr   => j < expr + 1
            remove_from = lex_end_site(t, -1)
            new = ' + 1'
            if t[5].kind == 'binop':
                (left, op, right) = t[5].args
                if op == '-' and right.kind == 'int' and right.args == (1,):
                    # j in 0..expr-1 => j < expr
                    remove_from = t[5].site
                    new = ''
        report(PARRAY(site(t, 2), site(t, 4), remove_from,
                      end_site(t[5].site), new))

@prod_dml14
def arraydef(t):
    '''arraydef : ident LT expression'''
    t[0] = (t[1], t[3])

# Traits

@prod_dml12
def toplevel_trait(t):
    'toplevel : TRAIT typeident maybe_istemplate LBRACE trait_stmts RBRACE'
    report(WEXPERIMENTAL(site(t), 'traits'))
    t[0] = ast.template(site(t), t[2], t[3] + t[5])

@prod_dml12
def trait_stmts_none(t):
    '''trait_stmts : '''
    t[0] = []

@prod_dml12
def trait_stmts(t):
    '''trait_stmts : trait_stmts trait_stmt'''
    t[0] = t[1] + t[2]

@prod_dml12
def trait_stmt(t):
    '''trait_stmt : trait_method
                  | trait_param'''
    t[0] = [t[1]]

@prod_dml12
def trait_stmt_istemplate(t):
    '''trait_stmt : istemplate SEMI'''
    t[0] = [t[1]]

@prod_dml12
def trait_session(t):
    # We don't support session variable initializers yet, because it's
    # not sufficiently obvious in what scope they should be evaluated.
    'trait_stmt : SESSION named_cdecl SEMI'
    (_, _, name, typ) = t[2]
    t[0] = [ast.session(site(t), name, typ, None)]

@prod_dml14
def template_stmts_none(t):
    '''template_stmts : '''
    t[0] = []

@prod_dml14
def template_stmts_some(t):
    '''template_stmts : template_stmts template_stmt'''
    t[0] = t[1] + t[2]

@prod_dml14
def template_statement_obj(t):
    '''template_stmt : object_statement_or_typedparam'''
    t[0] = [t[1]]

@prod_dml14
def template_statement_shared_method(t):
    '''template_stmt : SHARED METHOD shared_method'''
    t[0] = [t[3]]

@prod_dml12
def trait_template(t):
    '''trait_stmt : TEMPLATE LBRACE object_statements RBRACE'''
    for stmt in t[3]:
        if stmt.kind == 'is':
            # 'is' in a trait block means the same as it would have
            # meant in its template block, and it's confusing to have
            # two syntaxes for the same thing.
            report(EISINTPL(stmt.site))
    t[0] = t[3]

@prod_dml12
def trait_method(t):
    '''trait_method : METHOD shared_method'''
    t[0] = t[2]

@prod
def shared_method_abstract(t):
    '''shared_method : ident method_params_typed SEMI'''
    (inp, outp, throws) = t[2]
    t[0] = ast.sharedmethod(site(t), t[1], inp, outp, throws, True, None,
                            site(t, 3))

@prod
def shared_method_default(t):
    '''shared_method : ident method_params_typed DEFAULT compound_statement'''
    (inp, outp, throws) = t[2]
    t[0] = ast.sharedmethod(site(t), t[1], inp, outp, throws, True, t[4],
                            lex_end_site(t, -1))

@prod
def shared_method_final(t):
    '''shared_method : ident method_params_typed compound_statement'''
    (inp, outp, throws) = t[2]
    t[0] = ast.sharedmethod(site(t), t[1], inp, outp, throws, False, t[3],
                            lex_end_site(t, -1))

@prod_dml12
def param_(t):
    '''param_ : PARAMETER'''
    if logging.show_porting:
        report(PPARAMETER(site(t)))
    t[0] = t[1]

@prod_dml14
def param_(t):
    '''param_ : PARAM'''
    t[0] = t[1]

@prod_dml12
def trait_param(t):
    '''trait_param : param_ named_cdecl SEMI'''
    (name, typ) = t[2].args
    t[0] = ast.typedparam(site(t), t[2].site, name, typ)

# Templates

@prod_dml12
def template(t):
    'toplevel : TEMPLATE objident maybe_istemplate object_spec'
    t[0] = ast.template_dml12(site(t), t[2], t[3] + t[4])

@prod_dml14
def template(t):
    'toplevel : TEMPLATE objident maybe_istemplate LBRACE template_stmts RBRACE'
    ises = [s for s in t[5] if s.kind == 'is']
    shared_methods = [s for s in t[5] if s.kind == 'sharedmethod']
    if ises and shared_methods:
        report(WTEMPLATEIS(ises[0].site))
    t[0] = ast.template(site(t), t[2], t[3] + t[5])

# Header/footer

@prod
def header(t):
    'toplevel : HEADER CBLOCK'
    t[0] = ast.header(site(t), t[2], False)

@prod
def footer(t):
    'toplevel : FOOTER CBLOCK'
    t[0] = ast.footer(site(t), t[2])

@prod
def impl_header(t):
    'toplevel : _HEADER CBLOCK'
    t[0] = ast.header(site(t), t[2], True)

# loggroup

@prod
def loggroup(t):
    'toplevel : LOGGROUP ident SEMI'
    t[0] = ast.loggroup(site(t), t[2])

# constant/extern

@prod
def constant(t):
    'toplevel : CONSTANT ident EQUALS expression SEMI'
    t[0] = ast.constant(site(t), t[2], t[4])
    if logging.show_porting:
        report(PCONSTANT(site(t)))

@prod_dml12
def extern(t):
    'toplevel : EXTERN cdecl_or_ident SEMI'
    t[0] = ast.extern(site(t), t[2])

@prod_dml14
def extern(t):
    'toplevel : EXTERN cdecl SEMI'
    t[0] = ast.extern(site(t), t[2])

@prod
def typedef(t):
    'toplevel : TYPEDEF named_cdecl SEMI'
    t[0] = ast.dml_typedef(site(t), t[2])

@prod
def extern_typedef(t):
    'toplevel : EXTERN TYPEDEF named_cdecl SEMI'
    t[0] = ast.extern_typedef(site(t), t[3])

@prod_dml12
def top_struct(t):
    'toplevel : STRUCT ident LBRACE struct_decls RBRACE'
    if logging.show_porting:
        report(PSTRUCTDECL(site(t, 1), site(t, 2), site(t, 5)))
    t[0] = ast.dml_typedef(site(t), ast.cdecl(site(t), t[2],
                                              [('struct', t[4])]))

# Imports

@prod
def import_str(t):
    'toplevel : IMPORT utf8_sconst SEMI'
    t[0] = ast.import_(site(t), t[2])

# Common part of most object definitions
@prod
def object_desc(t):
    'object_desc : composed_string_literal'
    t[0] = [ast.parameter(site(t), 'desc',
                          (ast.string(site(t), t[1]), None, None))]
@prod
def object_desc_none(t):
    'object_desc :'
    t[0] = []

@prod
def object_spec_none(t):
    'object_spec : object_desc SEMI'
    t[0] = t[1]

@prod
def object_spec(t):
    'object_spec : object_desc LBRACE object_statements RBRACE'
    t[0] = t[1] + t[3]

@prod
def object_statements(t):
    'object_statements : object_statements object_statement'
    t[0] = t[1] + [t[2]]

@prod
def object_statements_empty(t):
    'object_statements : '
    t[0] = []

@prod
def object_statement(t):
    '''object_statement : object_statement_or_typedparam'''
    if t[1].kind == 'typedparam':
        report(ESYNTAX(site(t), ':',
                       'parameter type declaration only permitted'
                       + ' in top level template block'))
        # fallback: dummy statement
        t[0] = ast.hashif(site(t), ast.variable(site(t), 'false'), [], [])
    else:
        t[0] = t[1]

@prod_dml14
def object_statement_bad_shared_method(t):
    '''object_statement : bad_shared_method'''
    t[0] = t[1]

@prod_dml14
def bad_shared_method(t):
    '''bad_shared_method : SHARED METHOD shared_method'''
    report(ESYNTAX(site(t), 'shared',
                   'shared method declaration only permitted'
                   + ' in top level template block'))
    # fallback: dummy statement
    t[0] = ast.hashif(site(t), ast.variable(site(t), 'false'), [], [])

@prod_dml12
def object_statement_or_typedparam(t):
    '''object_statement_or_typedparam : object
                                      | parameter
                                      | method
                                      | istemplate SEMI
                                      | object_if
                                      | error_stmt'''
    t[0] = t[1]

@prod_dml14
def export(t):
    'toplevel : EXPORT expression AS expression SEMI'
    t[0] = ast.export(site(t), t[2], t[4]);

@prod_dml14
def object_statement_or_typedparam(t):
    '''object_statement_or_typedparam : object
                                      | parameter
                                      | method
                                      | istemplate SEMI
                                      | object_if
                                      | error_stmt
                                      | in_each'''
    t[0] = t[1]

@prod_dml14
def in_each(t):
    'in_each : IN EACH istemplate_list LBRACE object_statements RBRACE'
    t[0] = ast.in_each(site(t), [tpl for (_, tpl) in t[3]], t[5])

def validate_if_body(stmts):
    '''Report errors for statements that are illegal in an object-if body,
    and return a new list where illegal statements are filtered
    out'''
    result = []
    for stmt in stmts:
        if stmt.kind in allowed_in_hashif:
            result.append(stmt)
        elif stmt.kind == 'parameter':
            report(ECONDP(stmt.site))
        elif stmt.kind == 'is':
            report(ECONDT(stmt.site))
        elif stmt.kind == 'in_each':
            report(ECONDINEACH(stmt.site))
        else:
            raise ICE(stmt.site, 'unknown kind %r' % (stmt.kind,))
    return result

@prod_dml12
def hashif(t):
    '''hashif : IF'''
    if logging.show_porting:
        report(PHASH(site(t)))

@prod_dml14
def hashif(t):
    '''hashif : HASHIF'''

@prod_dml14
def hashif_nohash(t):
    '''hashif : IF'''
    report(ESYNTAX(site(t), 'if', "invalid 'if', use '#if' in object context"))

@prod_dml12
def hashelse(t):
    '''hashelse : ELSE'''
    if logging.show_porting:
        report(PHASH(site(t)))

@prod_dml14
def hashelse(t):
    '''hashelse : HASHELSE'''

@prod_dml14
def hashelse_nohash(t):
    '''hashelse : ELSE'''
    report(ESYNTAX(site(t), 'else', "invalid 'else', use '#else'"))

@prod
def object_if(t):
    '''object_if : hashif LPAREN expression RPAREN \
                   LBRACE object_statements RBRACE object_else'''
    t[0] = ast.hashif(site(t), t[3], validate_if_body(t[6]), t[8])

@prod
def object_else_no(t):
    '''object_else :'''
    t[0] = []

@prod
def object_else_body(t):
    '''object_else : hashelse LBRACE object_statements RBRACE'''
    t[0] = validate_if_body(t[3])

@prod
def object_else_if(t):
    '''object_else : hashelse object_if'''
    t[0] = [t[2]]

# Parameter specification
@prod
def object_parameter(t):
    '''parameter : param_ objident paramspec'''
    if logging.show_porting:
        if t[2] == 'hard_reset_value':
            report(PHARD_RESET_VALUE(site(t, 2)))
        if t[2] == 'soft_reset_value' and t[3][0]:
            report(PSOFT_RESET_VALUE(site(t)))
        if t[2] == 'miss_pattern':
            report(PMISS_PATTERN(site(t, 2)))
    t[0] = ast.parameter(site(t), t[2], t[3])

@prod_dml14
def object_typedparam(t):
    '''parameter : param_ objident COLON ctypedecl SEMI'''
    t[0] = ast.typedparam(site(t), site(t, 2), t[2], t[4])

@prod
def paramspec_empty(t):
    'paramspec : SEMI'
    t[0] = (None, None, None)

@prod
def paramspec_value(t):
    'paramspec : EQUALS expression SEMI'
    t[0] = (t[2], None, None)

@prod
def paramspec_default(t):
    'paramspec : DEFAULT expression SEMI'
    t[0] = (None, t[2], None)

@prod
def paramspec_auto(t):
    'paramspec : AUTO SEMI'
    t[0] = (None, None, 1)

# Method parameters
@prod
def method_outparams_none(t):
    'method_outparams : '
    t[0] = []

@prod_dml12
def method_outparams_some(t):
    'method_outparams : ARROW LPAREN cdecl_or_ident_list RPAREN'
    t[0] = t[3]

@prod_dml14
def method_outparams_some(t):
    'method_outparams : ARROW LPAREN cdecl_list RPAREN'
    for (i, (kind, psite, name, typ)) in enumerate(t[3]):
        if name:
            # It would be logical to just use ESYNTAX here, but be nicer
            # because this is a very common mistake until 1.2 is
            # deprecated
            report(ERETARGNAME(psite, name))
            t[3][i] = ast.cdecl(psite, None, typ)
    t[0] = t[3]

@prod_dml14
def method_params_maybe_untyped(t):
    'method_params_maybe_untyped : LPAREN cdecl_or_ident_list RPAREN method_outparams throws'
    t[0] = (t[2], t[4], t[5])

@prod
def method_params_typed(t):
    'method_params_typed : LPAREN cdecl_list RPAREN method_outparams throws'
    for (i, (kind, psite, name, typ)) in enumerate(t[2]):
        assert kind == 'cdecl'
        if not name:
            report(ESYNTAX(psite, None,
                           'name omitted in parameter declaration'))
            t[2][i] = ast.cdecl(psite, '_name_omitted%d' % (i,), typ)
    t[0] = (t[2], t[4], t[5])

@prod_dml12
def maybe_nothrow_throws(t):
    'maybe_nothrow : '
    t[0] = True

@prod_dml12
def maybe_nothrow_nothrow(t):
    'maybe_nothrow : NOTHROW'
    if logging.show_porting:
        report(PNOTHROW(site(t)))
    t[0] = False

@prod
def throws(t):
    'throws : THROWS'
    t[0] = True

@prod
def throws_not(t):
    'throws : '
    t[0] = False

# Method arguments

@prod_dml12
def returnargs_empty(t):
    'returnargs : '
    t[0] = []

@prod_dml12
def returnargs(t):
    'returnargs : ARROW LPAREN expression_list RPAREN'
    t[0] = t[3]

# Optional "is (read-only)" part
@prod
def maybe_istemplate_no(t):
    'maybe_istemplate : '
    t[0] = []

@prod
def maybe_istemplate_yes(t):
    'maybe_istemplate : istemplate'
    t[0] = [t[1]]

@prod
def istemplate(t):
    'istemplate : IS istemplate_list'
    t[0] = ast.is_(site(t), t[2])

@prod
def istemplate_list_one(t):
    'istemplate_list : objident'
    t[0] = [(site(t), t[1])]

@prod
def istemplate_list_multi(t):
    'istemplate_list : LPAREN objident_list RPAREN'
    t[0] = t[2]

# The shorthand size of a register
@prod
def sizespec(t):
    'sizespec : SIZE expression'
    t[0] = [ast.parameter(site(t), 'size', (t[2], None, None))]

@prod
def sizespec_empty(t):
    'sizespec : '
    t[0] = []

# The shorthand offset of a register
@prod
def offsetspec(t):
    'offsetspec : AT expression'
    t[0] = [ast.parameter(site(t), 'offset', (t[2], None, None))]

@prod
def offsetspec_empty(t):
    'offsetspec : '
    t[0] = []

# A C-like declaration, or a simple name
@prod_dml12
def cdecl_or_ident_decl(t):
    '''cdecl_or_ident : cdecl'''
    (_, site, name, typ) = t[1]
    if name:
        t[0] = t[1]
    elif len(typ) == 1:
        # Hack: a single identifier is parsed as an anonymous
        # parameter; i.e., a simple type ident with no name attached
        # to it. We convert that to an untyped identifier.
        t[0] = ast.cdecl(site, typ[0], None)
    else:
        raise ESYNTAX(site, None, "missing parameter name")

@prod_dml14
def cdecl_or_ident_decl(t):
    '''cdecl_or_ident : named_cdecl'''
    t[0] = t[1]

@prod_dml14
def cdecl_or_ident_inline(t):
    '''cdecl_or_ident : INLINE ident'''
    t[0] = ast.cdecl(site(t), t[2], None)

# A C-like declaration with required identifier name
@prod
def named_cdecl(t):
    '''named_cdecl : cdecl'''
    _, site, name, typ = t[1]

    if name:
        t[0] = t[1]
    else:
        report(ESYNTAX(site, None, "missing name in declaration"))
        t[0] = ast.cdecl(site, '_name_omitted', typ)

# A C-like declaration
@prod
def cdecl(t):
    '''cdecl : basetype cdecl2'''
    # t[2] is a list of modifiers, innermost last
    name = t[2][-1]
    info = [t[1]] + t[2][:-1]
    t[0] = ast.cdecl(site(t), name, info)

@prod
def cdecl_const(t):
    '''cdecl : CONST basetype cdecl2'''
    # t[2] is a list of modifiers, innermost last
    name = t[3][-1]
    info = [t[2], 'const'] + t[3][:-1]
    t[0] = ast.cdecl(site(t), name, info)

@prod
def basetype(t):
    '''basetype : typeident
                | struct
                | layout
                | bitfields
                | typeof'''
    t[0] = t[1]

@prod_dml14
def basetype_each(t):
    '''basetype : SEQUENCE LPAREN typeident RPAREN'''
    t[0] = ('sequence', t[3])

@prod
def cdecl2(t):
    'cdecl2 : cdecl3'
    t[0] = t[1]

@prod
def cdecl2_const(t):
    'cdecl2 : CONST cdecl2'
    t[0] = ['const'] + t[2]

@prod
def cdecl2_ptr(t):
    'cdecl2 : TIMES cdecl2'
    t[0] = ['pointer'] + t[2]

@prod_dml14
def cdecl2_vect(t):
    'cdecl2 : VECT cdecl2'
    # vect is actually experimental in 1.2 as well, but we will probably
    # defensively keep it the way it is, because it's used a lot.
    # TODO: improve how vect works in 1.4, and make it public (US325)
    report(WEXPERIMENTAL(site(t), 'vect types'))
    t[0] = ['vect'] + t[2]

@prod_dml12
def cdecl2_vect(t):
    'cdecl2 : VECT cdecl2'
    t[0] = ['vect'] + t[2]

@prod_dml12
def cdecl3(t):
    # Madness! we actually allow the declaration 'local int int;'.
    # The declaration 'data int int;' is also accepted, but gives
    # invalid C code.
    'cdecl3 : typeident'
    t[0] = [t[1]]

@prod_dml14
def cdecl3(t):
    'cdecl3 : ident'
    t[0] = [t[1]]

@prod
def cdecl3_empty(t):
    'cdecl3 : '
    t[0] = [None]

@prod
def cdecl3_arr(t):
    'cdecl3 : cdecl3 LBRACKET expression RBRACKET'
    t[0] = ['array', t[3]] + t[1]

@prod
def cdecl3_fun(t):
    'cdecl3 : cdecl3 LPAREN cdecl_list_opt_ellipsis RPAREN'
    t[0] = ['funcall', t[3]] + t[1]

@prod
def cdecl3_par(t):
    'cdecl3 : LPAREN cdecl2 RPAREN'
    t[0] = t[2]

# A comma-separated cdecl list, used in function parameter lists
@prod
def cdecl_list_empty(t):
    'cdecl_list : '
    t[0] = []

@prod
def cdecl_list_nonempty(t):
    'cdecl_list : cdecl_list_nonempty'
    t[0] = t[1]

@prod
def cdecl_list_one(t):
    'cdecl_list_nonempty : cdecl'
    t[0] = [t[1]]

@prod
def cdecl_list_many(t):
    '''cdecl_list_nonempty : cdecl_list_nonempty COMMA cdecl'''
    t[0] = t[1] + [t[3]]

# Variant that allows ELLIPSIS in the end
@prod
def cdecl_list_opt_ellipsis(t):
    '''cdecl_list_opt_ellipsis : cdecl_list
                               | cdecl_list_ellipsis'''
    t[0] = t[1]

@prod
def cdecl_list_ellipsis_only(t):
    'cdecl_list_ellipsis : ELLIPSIS'
    t[0] = [t[1]]

@prod
def cdecl_list_ellipsis_last(t):
    'cdecl_list_ellipsis : cdecl_list_nonempty COMMA ELLIPSIS'
    t[0] = t[1] + [t[3]]

# A comma-separated cdecl_or_ident list, used in method parameter lists
@prod
def cdecl_or_ident_list_empty(t):
    'cdecl_or_ident_list : '
    t[0] = []

@prod
def cdecl_or_ident_list_nonempty(t):
    'cdecl_or_ident_list : cdecl_or_ident_list2'
    t[0] = t[1]

@prod
def cdecl_or_ident_list2_one(t):
    'cdecl_or_ident_list2 : cdecl_or_ident'
    t[0] = [t[1]]

@prod
def cdecl_or_ident_list2(t):
    'cdecl_or_ident_list2 : cdecl_or_ident_list2 COMMA cdecl_or_ident'
    t[0] = t[1] + [t[3]]

@prod
def typeof(t):
    'typeof : TYPEOF expression'
    t[0] = ('typeof', t[2])

def check_struct_namecoll(member_decls):
    sites_by_name = {}
    for decl in member_decls:
        (name, _) = decl.args
        if name in sites_by_name:
            report(ENAMECOLL(decl.site, sites_by_name[name], name))
        else:
            sites_by_name[name] = decl.site

@prod
def struct(t):
    'struct : STRUCT LBRACE struct_decls RBRACE'
    check_struct_namecoll(t[3])
    t[0] = ('struct', t[3])

@prod
def struct_decls(t):
    'struct_decls : struct_decls named_cdecl SEMI'
    t[0] = t[1] + (t[2],)

@prod
def struct_decls_empty(t):
    'struct_decls : '
    t[0] = ()

@prod
def layout_decl(t):
    'layout_decl : LAYOUT utf8_sconst LBRACE layout_decls RBRACE'
    endian = t[2]
    if dml.globals.compat_dml12_int(site(t, 2)):
        field_names = set()
        fields = []
        for cdecl in t[4]:
            (name, typ) = cdecl.args
            if name in field_names:
                while (name in field_names
                       or any(name == d.args[0] for d in t[4])):
                    name = '_' + name
                cdecl = ast.cdecl(cdecl.site, name, typ)
            fields.append(cdecl)
            field_names.add(name)
    else:
        fields = t[4]
        check_struct_namecoll(fields)
    if endian not in ('big-endian', 'little-endian'):
        raise ESYNTAX(site(t, 2), '"%s"' % endian,
                      'not one of "big-endian" or "little-endian"')

    t[0] = (endian, fields)

@prod
def layout(t):
    'layout : layout_decl'
    t[0] = ('layout', t[1])

@prod
def layout_decls(t):
    'layout_decls : layout_decls named_cdecl SEMI'
    t[0] = t[1] + (t[2],)

@prod
def layout_decls_empty(t):
    'layout_decls : '
    t[0] = ()

@prod
def bitfields(t):
    'bitfields : BITFIELDS ICONST LBRACE bitfields_decls RBRACE'
    if t.parser.file_info.bitorder == 'be':
        width_expr = ast.int(site(t, 2), t[2])
        fields = [(decl,
                   endian_translate_bit(msb, width_expr, 'be'),
                   endian_translate_bit(lsb, width_expr, 'be'))
                  for (decl, msb, lsb) in t[4]]
    else:
        fields = t[4]
    if dml.globals.compat_dml12_int(site(t, 2)):
        field_names = set()
        fields_nodup = []
        for f in fields:
            (cdecl, _, _) = f
            (name, _) = cdecl.args
            if name not in field_names:
                field_names.add(name)
                fields_nodup.append(f)
        fields = fields_nodup
    else:
        check_struct_namecoll((f for (f, _, _) in fields))
    t[0] = ('bitfields', (t[2], fields))

@prod
def bitfields_decls(t):
    'bitfields_decls : bitfields_decls named_cdecl AT LBRACKET bitfield_range RBRACKET SEMI'
    t[0] = t[1] + ((t[2], *t[5]),)

@prod
def bitfield_range_1(t):
    'bitfield_range : expression'
    t[0] = (t[1], t[1])

@prod
def bitfield_range_2(t):
    'bitfield_range : expression COLON expression'
    t[0] = (t[1], t[3])

@prod
def bitfields_decls_empty(t):
    'bitfields_decls : '
    t[0] = ()

# ctypedecl is a type without any declared variable
@prod
def ctypedecl(t):
    'ctypedecl : const_opt basetype ctypedecl_ptr'
    t[0] = [t[2]] + t[1] + t[3]

@prod
def ctypedecl_ptr(t):
    'ctypedecl_ptr : stars ctypedecl_array'
    t[0] = t[2] + t[1]

@prod
def stars_empty(t):
    'stars : '
    t[0] = []

@prod
def stars_const(t):
    'stars : TIMES CONST stars'
    t[0] = t[3] + ['const', 'pointer']

@prod
def stars(t):
    'stars : TIMES stars'
    t[0] = t[2] + ['pointer']

# This rule is in conflict with p_expression_new_array, so leave it
# out for now
#
#@prod
#def ctypedecl_array(t):
#    'ctypedecl_array : ctypedecl_array LBRACKET RBRACKET'
#    t[0] = t[1] + ['array', None]

@prod
def ctypedecl_array_simple(t):
    'ctypedecl_array : ctypedecl_simple'
    t[0] = t[1]

@prod
def ctypedecl_simple_par(t):
    'ctypedecl_simple : LPAREN ctypedecl_ptr RPAREN'
    t[0] = t[2]

@prod
def ctypedecl_simple_none(t):
    'ctypedecl_simple : ' # no variable here
    t[0] = []

@prod
def const_opt(t):
    'const_opt : CONST'
    t[0] = ['const']

@prod
def const_opt_empty(t):
    'const_opt :'
    t[0] = []

@prod
def typeident(t):
    '''typeident : ident
                 | CHAR
                 | DOUBLE
                 | FLOAT
                 | INT
                 | LONG
                 | SHORT
                 | SIGNED
                 | UNSIGNED
                 | VOID
                 | REGISTER'''
    t[0] = t[1]

# expression

@prod_dml12
def expression_assign(t):
    'expression : expression EQUALS expression'
    t[0] = ast.set(site(t, 2), t[1], t[3])

@prod
def assignop(t):
    '''assignop : expression PLUSEQUAL expression
                | expression MINUSEQUAL expression
                | expression TIMESEQUAL expression
                | expression DIVEQUAL expression
                | expression MODEQUAL expression
                | expression BOREQUAL expression
                | expression BANDEQUAL expression
                | expression BXOREQUAL expression
                | expression LSHIFTEQUAL expression
                | expression RSHIFTEQUAL expression'''
    t[0] = ast.assignop(site(t, 2), t[1], t[2], t[3])

@prod_dml12
def expression_assignop(t):
    '''expression : assignop'''
    (tgt, op, src) = t[1].args
    t[0] = ast.set(t[1].site, tgt, ast.binop(t[1].site, tgt, op[:-1], src))

@prod
def expression_conditional(t):
    'expression : expression CONDOP expression COLON expression'
    t[0] = ast.conditional(site(t, 2), t[1], t[3], t[5])

@prod_dml14
def expression_hash_conditional(t):
    'expression : expression HASHCONDOP expression HASHCOLON expression'
    t[0] = ast.hashcond(site(t, 2), t[1], t[3], t[5])

@prod
def expression_binary_operator(t):
    '''expression : expression PLUS expression
                  | expression MINUS expression
                  | expression TIMES expression
                  | expression DIVIDE expression
                  | expression MOD expression
                  | expression LSHIFT expression
                  | expression RSHIFT expression
                  | expression EQ expression
                  | expression NE expression
                  | expression LT expression
                  | expression GT expression
                  | expression LE expression
                  | expression GE expression
                  | expression LOR expression
                  | expression LAND expression
                  | expression BOR expression
                  | expression BXOR expression
                  | expression BAND expression'''
    t[0] = ast.binop(site(t, 2), t[1], t[2], t[3])

# TODO: proper C-cast

@prod
def expression_cast(t):
    'expression : CAST LPAREN expression COMMA ctypedecl RPAREN'
    t[0] = ast.cast(site(t), t[3], t[5])

# TODO: proper C-sizeof

@prod
def expression_sizeof(t):
    'expression : SIZEOF expression'
    t[0] = ast.unop(site(t), t[1], t[2])

@prod
def expression_unary_operator(t):
    '''expression : MINUS expression %prec unary_prefix
                  | PLUS expression %prec unary_prefix
                  | LNOT expression
                  | BNOT expression
                  | BAND expression %prec unary_prefix
                  | TIMES expression %prec unary_prefix
                  | DEFINED expression'''
    t[0] = ast.unop(site(t), t[1], t[2])

@prod_dml12
def expression_hash(t):
    '''expression : HASH expression'''
    if logging.show_porting:
        report(PSTRINGIFY(site(t, 1), end_site(site(t, 2))))
    t[0] = ast.unop(site(t), "stringify", t[2])

@prod_dml14
def expression_stringify(t):
    '''expression : STRINGIFY LPAREN expression RPAREN'''
    t[0] = ast.unop(site(t), t[1], t[3])

@prod
def expression_preincdec(t):
    '''expression : PLUSPLUS expression
                  | MINUSMINUS expression'''
    t[0] = ast.unop(site(t), t[1], t[2])

@prod
def expression_postincdec(t):
    '''expression : expression PLUSPLUS %prec unary_postfix
                  | expression MINUSMINUS %prec unary_postfix'''
    t[0] = ast.unop(site(t), 'post' + t[2], t[1])

@prod
def application(t):
    'expression : expression LPAREN expression_list RPAREN'
    t[0] = ast.apply(site(t), t[1], t[3])

@prod
def expression_int(t):
    '''expression : ICONST
                  | HCONST
                  | BCONST
                  | CCONST'''
    t[0] = ast.int(site(t), t[1])

@prod
def expression_float(t):
    'expression : FCONST'
    t[0] = ast.float(site(t), t[1])

@prod
def expression_string(t):
    'expression : SCONST'
    t[0] = ast.string(site(t), t[1])

@prod
def utf8_string(t):
    'utf8_sconst : SCONST'
    try:
        t[0] = t[1].decode('utf-8')
    except UnicodeDecodeError as e:
        raise ESYNTAX(
            site(t),
            repr(t),
            'utf-8 decoding error: ' + e.reason)

@prod
def expression_undefined(t):
    'expression : UNDEFINED'
    t[0] = ast.undefined(site(t))

@prod_dml12
def expression_objectref(t):
    'expression : DOLLAR objident'
    t[0] = ast.objectref(site(t, 2), t[2])
    if logging.show_porting:
        report(PNODOLLAR(site(t, 1)))

@prod_dml12
def expression_ident(t):
    '''expression : objident
                  | DEFAULT'''
    t[0] = ast.variable_dml12(site(t), t[1])

@prod_dml14
def expression_ident(t):
    '''expression : objident
                  | DEFAULT'''
    t[0] = ast.variable(site(t), t[1])

@prod_dml14
def expression_this(t):
    '''expression : THIS'''
    t[0] = ast.variable(site(t), t[1])

@prod
def expression_member(t):
    '''expression : expression PERIOD objident
                  | expression ARROW objident'''
    t[0] = ast.member(site(t, 2), t[1], t[2], t[3])

@prod
def expression_typeop(t):
    '''expression : SIZEOFTYPE typeoparg'''
    t[0] = ast.typeop(site(t), t[2])

@prod
def typeop_arg(t):
    '''typeoparg : ctypedecl'''
    t[0] = t[1]

@prod
def typeop_arg_par(t):
    '''typeoparg : LPAREN ctypedecl RPAREN'''
    t[0] = t[2]

@prod
def expression_new(t):
    '''expression : NEW ctypedecl'''
    t[0] = ast.new(site(t), t[2], None)

@prod
def expression_new_array(t):
    '''expression : NEW ctypedecl LBRACKET expression RBRACKET'''
    t[0] = ast.new(site(t), t[2], t[4])

@prod
def expression_paren(t):
    'expression : LPAREN expression RPAREN'
    t[0] = t[2]

@prod
def expression_constlist(t):
    'expression : LBRACKET expression_list RBRACKET'
    t[0] = ast.list(site(t), t[2])

@prod
def expression_index(t):
    'expression : expression LBRACKET expression RBRACKET'
    t[0] = ast.index(site(t, 2), t[1], t[3], t[4])

@prod
def expression_slice_one_bit(t):
    'expression : expression LBRACKET expression COMMA ID RBRACKET'
    t[0] = ast.slice(site(t, 2), t[1], t[3], None, parse_bitorder(t, t[5]))

@prod
def expression_slice(t):
    'expression : expression LBRACKET expression COLON expression endianflag RBRACKET'
    t[0] = ast.slice(site(t, 2), t[1], t[3], t[5], t[6])

@prod_dml14
def expression_each_in(t):
    'expression : EACH objident IN LPAREN expression RPAREN'
    t[0] = ast.each_in(site(t), t[2], t[5])

@prod
def endianflag(t):
    'endianflag : COMMA ID'
    t[0] = parse_bitorder(t, t[2])

@prod
def endianflag_none(t):
    'endianflag : '
    t[0] = None

# expression-opt

@prod
def expression_opt_1(t):
    'expression_opt : expression'
    t[0] = t[1]

@prod
def expression_opt_2(t):
    'expression_opt : '
    t[0] = None

# A comma-separated expression list.  A trailing comma is allowed

@prod
def expression_list(t):
    'expression_list : '
    t[0] = []

@prod
def expression_list_one(t):
    'expression_list : expression'
    t[0] = [t[1]]

@prod
def expression_list_many(t):
    'expression_list : expression COMMA expression_list'
    t[0] = [t[1]] + t[3]

# A comma-separated expression list.  A trailing comma is NOT allowed.

@prod_dml12
def expression_list_ntc_empty(t):
    'expression_list_ntc : '
    t[0] = []

@prod_dml12
def expression_list_ntc_nonempty(t):
    'expression_list_ntc : expression_list_ntc_nonempty'
    t[0] = t[1]

@prod
def expression_list_ntc_nonempty_one(t):
    'expression_list_ntc_nonempty : expression'
    t[0] = [t[1]]

@prod
def expression_list_ntc_nonempty_many(t):
    'expression_list_ntc_nonempty : expression COMMA expression_list_ntc_nonempty'
    t[0] = [t[1]] + t[3]

# A composed string literal, consisting of one or more quoted strings,
# chained together with '+'.

@prod
def composed_string_lit_single(t):
    'composed_string_literal : utf8_sconst'
    t[0] = t[1]

@prod
def composed_string_lit(t):
    'composed_string_literal : composed_string_literal PLUS utf8_sconst'
    t[0] = t[1] + t[3]

@prod
def bracketed_string(t):
    'bracketed_string_literal : composed_string_literal'
    t[0] = t[1]

@prod
def bracketed_string_bracketed(t):
    '''bracketed_string_literal : LPAREN composed_string_literal RPAREN'''
    t[0] = t[2]

# data object initializer

@prod
def initializer_scalar(t):
    'initializer : expression'
    t[0] = ast.initializer_scalar(site(t), t[1])

@prod
def initializer_compound(t):
    '''initializer : LBRACE initializer_list RBRACE
                   | LBRACE initializer_list COMMA RBRACE'''
    t[0] = ast.initializer_compound(site(t), t[2])


@prod
def initializer_list_one(t):
    'initializer_list : initializer'
    t[0] = [t[1]]

@prod
def initializer_list_many(t):
    'initializer_list : initializer_list COMMA initializer'
    t[0] = t[1] + [t[3]]

@prod_dml14
def initializer_designated_struct(t):
    '''initializer : LBRACE designated_struct_initializer_list RBRACE
                   | LBRACE designated_struct_initializer_list COMMA RBRACE'''
    t[0] = ast.initializer_designated_struct(site(t), t[2])

@prod_dml14
def designated_struct_initializer(t):
    '''designated_struct_initializer : PERIOD ident EQUALS initializer'''
    t[0] = (t[2], t[4])

@prod_dml14
def designated_struct_initializer_list_one(t):
    '''designated_struct_initializer_list : designated_struct_initializer'''
    t[0] = [t[1]]

@prod_dml14
def designated_struct_initializer_list_many(t):
    '''designated_struct_initializer_list : designated_struct_initializer_list COMMA designated_struct_initializer'''
    t[0] = t[1] + [t[3]]


# statement

@prod
def statement_not_hashif(t):
    'statement : statement_except_hashif'
    t[0] = t[1]

@prod
def statement_compound(t):
    'statement_except_hashif : compound_statement'
    t[0] = t[1]

@prod
def statement_decl(t):
    'statement_except_hashif : local SEMI'
    t[0] = t[1]

@prod_dml14
def saved_stmt(t):
    'statement_except_hashif : saved_decl'
    name, typ, init = t[1].args
    t[0] = ast.saved_statement(site(t), name, typ, init)

@prod_dml14
def statement_assignment(t):
    '''statement_except_hashif : assign_stmt SEMI'''
    t[0] = t[1]

@prod_dml14
def statement_assignop(t):
    '''statement_except_hashif : assignop SEMI'''
    t[0] = t[1]

@prod_dml14
def assign_stmt(t):
    '''assign_stmt : assign_chain'''
    (tgts, src) = t[1]
    t[0] = ast.assign(site(t), tgts, src)

@prod_dml14
def assign_chain_many(t):
    '''assign_chain : assign_target EQUALS assign_chain'''
    (tgts, src) = t[3]
    t[0] = ([t[1]] + tgts, src)

@prod_dml14
def assign_chain_one(t):
    '''assign_chain : assign_target EQUALS initializer'''
    t[0] = ([t[1]], t[3])

@prod_dml14
def assign_target_one(t):
    '''assign_target : expression'''
    t[0] = [t[1]]

@prod_dml14
def assign_target_many(t):
    'assign_target : LPAREN expression COMMA expression_list_ntc_nonempty RPAREN'
    t[0] = [t[2]] + t[4]

@prod
def statement_null(t):
    'statement_except_hashif : SEMI'
    t[0] = ast.null(site(t))

@prod
def statement_expression(t):
    'statement_except_hashif : expression SEMI'
    t[0] = ast.expression(site(t, 2), t[1])

@prod
def statement_if(t):
    'statement_except_hashif : IF LPAREN expression RPAREN statement %prec LOWEST_PREC'
    t[0] = ast.if_(site(t), t[3], t[5], None)

@prod
def statement_ifelse(t):
    'statement_except_hashif : IF LPAREN expression RPAREN statement ELSE statement'
    t[0] = ast.if_(site(t), t[3], t[5], t[7])

@prod_dml14
def statement_hashif(t):
    'statement : HASHIF LPAREN expression RPAREN statement %prec LOWEST_PREC'
    t[0] = ast.hashif(site(t), t[3], t[5], None)

@prod_dml14
def statement_hashifelse(t):
    'statement : HASHIF LPAREN expression RPAREN statement HASHELSE statement'
    t[0] = ast.hashif(site(t), t[3], t[5], t[7])

@prod
def statement_while(t):
    'statement_except_hashif : WHILE LPAREN expression RPAREN statement'
    t[0] = ast.while_(site(t), t[3], t[5])

@prod
def statement_do(t):
    'statement_except_hashif : DO statement WHILE LPAREN expression RPAREN SEMI'
    t[0] = ast.dowhile(site(t), t[5], t[2])

# In DML 1.2, assignments are expressions, and pre/post actions are
# expression lists.
@prod_dml12
def statement_for(t):
    'statement_except_hashif : FOR LPAREN expression_list_ntc SEMI expression_opt SEMI expression_list_ntc RPAREN statement'
    post = [ast.expression(e.site, e) for e in t[7]]
    t[0] = ast.for_(site(t), t[3], t[5], post, t[9])

# In DML 1.4, assignments are statements, and pre/post actions are
# comma-separated statement lists
@prod_dml14
def for_post_empty(t):
    'for_post : '
    t[0] = []

@prod_dml14
def for_post_nonempty(t):
    'for_post : for_post_nonempty'
    t[0] = t[1]

@prod_dml14
def for_post_one(t):
    'for_post_nonempty : for_post_one'
    t[0] = [t[1]]

@prod_dml14
def for_post_many(t):
    'for_post_nonempty : for_post_nonempty COMMA for_post_one'
    t[0] = t[1] + [t[3]]

@prod_dml14
def for_post_assign(t):
    '''for_post_one : assign_stmt'''
    t[0] = t[1]

@prod_dml14
def for_post_assignop(t):
    '''for_post_one : assignop'''
    t[0] = t[1]

@prod_dml14
def for_post_expr(t):
    '''for_post_one : expression'''
    t[0] = ast.expression(t[1].site, t[1])

@prod_dml14
def for_pre_local(t):
    '''for_pre : local'''
    t[0] = [t[1]]

@prod_dml14
def for_pre_other(t):
    '''for_pre : for_post'''
    t[0] = t[1]

@prod_dml14
def statement_for(t):
    'statement_except_hashif : FOR LPAREN for_pre SEMI expression_opt SEMI for_post RPAREN statement'
    s = site(t)
    t[0] = ast.compound(s, t[3] + [ast.for_(site(t), [], t[5], t[7], t[9])])

@prod_dml12
def statement_switch(t):
    'statement_except_hashif : SWITCH LPAREN expression RPAREN statement'
    t[0] = ast.switch(site(t), t[3], t[5])

@prod_dml14
def statement_switch(t):
    'statement_except_hashif : SWITCH LPAREN expression RPAREN LBRACE stmt_or_case_list RBRACE'
    stmts = t[6]
    t[0] = ast.switch(site(t), t[3], ast.compound(site(t, 5), stmts))

@prod_dml14
def stmt_or_case(t):
    '''stmt_or_case : statement_except_hashif
                    | cond_case_statement
                    | case_statement'''
    t[0] = t[1]

@prod_dml14
def switch_hashif(t):
    'cond_case_statement : HASHIF LPAREN expression RPAREN LBRACE stmt_or_case_list RBRACE %prec LOWEST_PREC'
    t[0] = ast.hashif(site(t), t[3], ast.compound(site(t, 5), t[6]), None)

@prod_dml14
def switch_hashifelse(t):
    'cond_case_statement : HASHIF LPAREN expression RPAREN LBRACE stmt_or_case_list RBRACE HASHELSE LBRACE stmt_or_case_list RBRACE'
    t[0] = ast.hashif(site(t), t[3], ast.compound(site(t, 5), t[6]),
                      ast.compound(site(t, 9), t[10]))

@prod_dml14
def stmt_or_case_list_empty(t):
    'stmt_or_case_list : '
    t[0] = []

@prod_dml14
def stmt_or_case_list_stmt(t):
    'stmt_or_case_list : stmt_or_case_list stmt_or_case'
    t[0] = t[1] + [t[2]]

# Delete is an expression in C++, not a statement, but we don't care.
@prod
def statement_delete(t):
    'statement_except_hashif : DELETE expression SEMI'
    t[0] = ast.delete(site(t), t[2])

@prod
def statent_try(t):
    'statement_except_hashif : TRY statement CATCH statement'
    t[0] = ast.try_(site(t), t[2], t[4])

@prod_dml12
def statement_delay(t):
    'statement_except_hashif : AFTER LPAREN expression RPAREN CALL expression SEMI'
    if logging.show_porting:
        if t[6].kind != 'apply':
            report(PINPARAMLIST(site(t, 7)))
        report(PAFTER(site(t, 5), site(t, 6), site(t, 2), site(t, 4)))
    t[0] = ast.after(site(t), 's', t[3], t[6])

@prod_dml14
def statement_delay(t):
    'statement_except_hashif : AFTER expression ID COLON expression SEMI'
    unit = t[3]
    # can easily be extended to other time units later
    supported_units = ['s', 'cycles']
    if unit not in supported_units:
        suggestions = ' or '.join(f"'{unit}'" for unit in supported_units)
        raise ESYNTAX(site(t, 3), t[3],
                      f"expected time unit ({suggestions})")
    t[0] = ast.after(site(t), unit, t[2], t[5])

@prod_dml12
def call(t):
    '''statement_except_hashif : CALL expression returnargs SEMI
                               | INLINE expression returnargs SEMI'''
    # This is a bit ugly.  The 'expression' can be a function
    # application, which will be treated as a method invocation instead.
    # If it is something else, it will be assumed to be a method
    # reference.
    if t[2].kind == 'apply':
        method_ast = t[2][2]
        inargs = t[2][3]
    else:
        if logging.show_porting:
            i = min([3, 4], key=lambda i: t.lexpos(i))
            report(PINPARAMLIST(site(t, i)))
        method_ast = t[2]
        inargs = []

    if logging.show_porting:
        report(PINVOKE(site(t, 1), site(t, 2), site(t, 3), site(t, 4),
                       len(t[3])))

    t[0] = ast.get(t[1])(site(t), method_ast, inargs, t[3])

@prod
def statement_assert(t):
    'statement_except_hashif : ASSERT expression SEMI'
    t[0] = ast.assert_(site(t), t[2])

log_types = {'spec_viol': 'spec_violation', 'error': 'error', 'info': 'info',
             'unimpl': 'unimplemented', 'critical': 'critical'}
@prod
def log_kind(t):
    '''log_kind : ID
                | ERROR'''
    lt = log_types.get(t[1], None)
    if lt is None:
        report(ELTYPE(site(t), t[1]))
        lt = 'info'
    t[0] = lt

@prod_dml14
def log_level_once(t):
    '''log_level : expression THEN expression'''
    t[0] = (t[1], t[3])

@prod
def log_level(t):
    '''log_level : expression '''
    t[0] = (t[1], None)

old_log_types = set(log_types.values())
@prod_dml12
def log_kind_old(t):
    'log_kind : utf8_sconst'
    lt = t[1]
    if lt not in old_log_types:
        report(ELTYPE(site(t), t[1]))
        lt = 'info'
    if logging.show_porting:
        [newkind] = [k for k in log_types if log_types[k] == lt]
        report(PLOGKIND(site(t, 1), '"%s"' % lt, newkind))
    t[0] = lt

@prod
def statement_log_1(t):
    'statement_except_hashif : LOG log_kind COMMA log_level COMMA expression COLON bracketed_string_literal log_args SEMI'
    kind = t[2]
    (level, later_level) = t[4]
    flags = t[6]
    fmt = t[8]
    args = t[9]
    t[0] = ast.log(site(t), kind, level, later_level, flags, fmt, args)

@prod
def statement_log_2(t):
    'statement_except_hashif : LOG log_kind COMMA log_level COLON bracketed_string_literal log_args SEMI'
    kind = t[2]
    (level, later_level) = t[4]
    flags = ast.int(site(t), 0)
    fmt = t[6]
    args = t[7]
    t[0] = ast.log(site(t), kind, level, later_level, flags, fmt, args)

@prod
def statement_log_3(t):
    'statement_except_hashif : LOG log_kind COLON bracketed_string_literal log_args SEMI'
    kind = t[2]
    level = ast.int(site(t), 1)
    later_level = None
    flags = ast.int(site(t), 0)
    fmt = t[4]
    args = t[5]
    t[0] = ast.log(site(t), kind, level, later_level, flags, fmt, args)

@prod_dml12
def hashselect(t):
    '''hashselect : SELECT'''
    if logging.show_porting:
        report(PHASH(site(t)))

@prod_dml14
def hashselect(t):
    '''hashselect : HASHSELECT'''

@prod
def select(t):
    'statement_except_hashif : hashselect ident IN LPAREN expression RPAREN WHERE LPAREN expression RPAREN statement hashelse statement'
    t[0] = ast.select(site(t), t[2], t[5], t[9], t[11], t[13])

@prod_dml12
def foreach(t):
    'statement_except_hashif : FOREACH ident IN LPAREN expression RPAREN statement'
    t[0] = ast.foreach_dml12(site(t), t[2], t[5], t[7])
    if logging.show_porting:
        report(PHASH(site(t)))

@prod_dml14
def foreach(t):
    'statement_except_hashif : FOREACH ident IN LPAREN expression RPAREN statement'
    t[0] = ast.foreach(site(t), t[2], t[5], t[7])

@prod_dml14
def hashforeach(t):
    'statement_except_hashif : HASHFOREACH ident IN LPAREN expression RPAREN statement'
    t[0] = ast.hashforeach(site(t), t[2], t[5], t[7])

@prod_dml12
def labeled_statement(t):
    'statement_except_hashif : ident COLON statement'
    t[0] = ast.label(site(t), t[1], t[3])

@prod_dml12
def statement_case(t):
    'statement_except_hashif : CASE expression COLON statement'
    t[0] = ast.case_dml12(site(t), t[2], t[4])

@prod_dml12
def statement_default(t):
    'statement_except_hashif : DEFAULT COLON statement'
    t[0] = ast.default_dml12(site(t), t[3])

@prod_dml14
def case_statement_case(t):
    'case_statement : CASE expression COLON'
    t[0] = ast.case(site(t), t[2])

@prod_dml14
def case_statement_default(t):
    'case_statement : DEFAULT COLON'
    t[0] = ast.default(site(t))

@prod_dml14
def goto_statement(t):
    'statement_except_hashif : GOTO ident SEMI'
    # Restricted goto should be implemented, see SIMICS-6130
    report(ESYNTAX(site(t), 'goto',
                  'goto statements are not yet implemented in DML 1.4'))
    t[0] = ast.null(site(t))

@prod_dml12
def goto_statement(t):
    'statement_except_hashif : GOTO ident SEMI'
    t[0] = ast.goto(site(t), t[2])

# Throw is an expression in C++, not a statement, but we don't care.
@prod
def simple_statement(t):
    '''statement_except_hashif : BREAK SEMI
                       | CONTINUE SEMI
                       | THROW SEMI'''
    t[0] = ast.get(t[1])(site(t))

@prod_dml12
def return_statement_noargs(t):
    '''statement_except_hashif : RETURN SEMI'''
    t[0] = ast.return_dml12(site(t), [])

@prod_dml14
def return_statement_noargs(t):
    '''statement_except_hashif : RETURN SEMI'''
    t[0] = ast.return_(site(t), [])

@prod_dml14
def return_statement_args(t):
    '''statement_except_hashif : RETURN assign_target SEMI'''
    t[0] = ast.return_(site(t), t[2])

@prod
def error_statement(t):
    'statement_except_hashif : error_stmt'
    t[0] = t[1]

@prod
def error_stmt_1(t):
    'error_stmt : ERROR SEMI'
    t[0] = ast.error(site(t), None)

@prod
def error_stmt_2(t):
    'error_stmt : ERROR bracketed_string_literal SEMI'
    t[0] = ast.error(site(t), t[2])

@prod
def warning_statement(t):
    'statement_except_hashif : warning_stmt'
    t[0] = t[1]

@prod
def warning_stmt(t):
    'warning_stmt : _WARNING bracketed_string_literal SEMI'
    report(WEXPERIMENTAL(site(t), "_warning statement"))
    t[0] = ast.warning(site(t), t[2])

# Format arguments for log statements
@prod
def log_args_empty(t):
    'log_args : '
    t[0] = []

@prod
def log_args(t):
    'log_args : log_args COMMA expression'
    t[0] = t[1] + [t[3]]

# compound-statement

@prod
def compound_statement_2(t):
    'compound_statement : LBRACE statement_list RBRACE'
    t[0] = ast.compound(site(t), t[2])

# statement-list

@prod
def statement_list_1(t):
    'statement_list : '
    t[0] = []

@prod
def statement_list_2(t):
    'statement_list : statement_list statement'
    t[0] = t[1] + [t[2]]

# local

@prod_dml12
def local_keyword_auto(t):
    '''local_keyword : AUTO'''
    if logging.show_porting:
        report(PAUTO(site(t)))
    t[0] = 'local'

@prod
def local_keyword(t):
    '''local_keyword : LOCAL'''
    t[0] = 'local'

@prod_dml14
def static(t):
    'static : SESSION'
    t[0] = 'session'

@prod_dml12
def static(t):
    'static : STATIC'
    if logging.show_porting:
        report(PSESSION(site(t), 'static', 'session'))
    t[0] = 'session'

@prod
def local(t):
    '''local : local_keyword cdecl
             | static cdecl'''
    (_, tsite, name, typ) = t[2]
    assert typ
    if not name:
        raise ESYNTAX(tsite, ";", "variable name omitted")
    t[0] = ast.get(t[1])(site(t), name, typ, None)

@prod
def local_static(t):
    '''local : local_keyword cdecl EQUALS initializer
             | static cdecl EQUALS initializer'''
    (_, _, name, typ) = t[2]
    assert typ
    if not name:
        raise ESYNTAX(site(t, 3), "=", "variable name omitted")
    t[0] = ast.get(t[1])(site(t), name, typ, t[4])

@prod
def objident_list_one(t):
    'objident_list : objident'
    t[0] = [(site(t), t[1])]

@prod
def objident_list(t):
    'objident_list : objident_list COMMA objident'
    t[0] = t[1] + [(site(t, 3), t[3])]

# Object/parameter names may use some additional keywords for now...
@prod_dml12
def objident(t):
    '''objident : ident
                | THIS
                | REGISTER
                | SIGNED
                | UNSIGNED'''
    t[0] = t[1]

@prod_dml14
def objident(t):
    '''objident : ident
                | REGISTER'''
    t[0] = t[1]

def ident_rule(idents):
    return 'ident : ' +  "\n| ".join(idents)

# Most DML top-level keywords are also allowed as identifiers.

@prod_dml12
@lex.TOKEN(ident_rule(dmllex12.reserved_idents + (
    'ID', 'EACH', 'SESSION', 'SEQUENCE')))
def ident(t):
    t[0] = t[1]

@prod_dml14
@lex.TOKEN(ident_rule(dmllex14.reserved_idents + ('ID',)))
def ident(t):
    t[0] = t[1]

reserved_words_12 = [
    'CLASS', 'ENUM', 'NAMESPACE', 'PRIVATE', 'PROTECTED', 'PUBLIC',
    'RESTRICT', 'UNION', 'USING', 'VIRTUAL', 'VOLATILE']

reserved_words_14 = reserved_words_12 + ['CALL', 'AUTO',
                                         'STATIC', 'SELECT',
                                         'ASYNC', 'AWAIT', 'WITH']

@prod_dml12
@lex.TOKEN(ident_rule(reserved_words_12))
def reserved(t):
    raise ESYNTAX(site(t, 1), str(t[1]), "reserved word")

@prod_dml14
@lex.TOKEN(ident_rule(reserved_words_14))
def reserved(t):
    raise ESYNTAX(site(t, 1), str(t[1]), "reserved word")

# Error handling
@prod
def error(t):
    if t and t.value:
        value = str(t.value)
        raise ESYNTAX(DumpableSite(t.lexer.file_info, t.lexpos), value, None)
    else:
        # raise ESYNTAX(...) would have made sense here, but there
        # is no clean way to retrieve the file (because t is
        # None). Therefore, push the error reporting responsibility to
        # whoever invokes the parser.
        raise UnexpectedEOF()

# Specific grammars to be passed to ply
class Grammar(object):
    def __init__(self, tokens, precedence, rules):
        self.precedence = precedence
        self.tokens = tokens
        self.__dict__.update(iter(list(rules.items())))

grammars = {(1, 2): Grammar(dmllex12.tokens, precedence((1, 2)),
                            production_rules_dml12),
            (1, 4): Grammar(dmllex14.tokens, precedence((1, 4)),
                            production_rules_dml14)}

lexers = {
    (1, 2): dmllex12,
    (1, 4): dmllex14,
}
