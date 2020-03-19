from . import log
from .constants import NO, MOD, YES

import subprocess
import os
import re
import kconfiglib
from kconfiglib import expr_value

from sympy import pretty, Symbol, true, false, Or, And, Not
from sympy.logic.boolalg import to_cnf, simplify_logic
from sympy.logic.inference import satisfiable


def tri_to_bool(tri):
    """
    Converts a tristate to a boolean value (['n'] -> False, ['m', 'y'] -> True)
    """
    return tri != NO

def expr_value_bool(expr):
    """
    Evaluates the given expression using expr_value(expr) and converts
    the result to a boolean value using tri_to_bool().
    """
    return tri_to_bool(expr_value(expr))

def set_env_default(var, default_value):
    """
    Sets an environment variable to the given default_value if it is currently unset.
    """
    if var not in os.environ:
        os.environ[var] = default_value

def detect_arch():
    arch = subprocess.run(['uname', '-m'], stdout=subprocess.PIPE).stdout.decode().strip().splitlines()[0]
    arch = re.sub('i.86',      'x86',     arch)
    arch = re.sub('x86_64',    'x86',     arch)
    arch = re.sub('sun4u',     'sparc64', arch)
    arch = re.sub('arm.*',     'arm',     arch)
    arch = re.sub('sa110',     'arm',     arch)
    arch = re.sub('s390x',     's390',    arch)
    arch = re.sub('parisc64',  'parisc',  arch)
    arch = re.sub('ppc.*',     'powerpc', arch)
    arch = re.sub('mips.*',    'mips',    arch)
    arch = re.sub('sh[234].*', 'sh',      arch)
    arch = re.sub('aarch64.*', 'arm64',   arch)
    arch = re.sub('riscv.*',   'riscv',   arch)
    return arch

kernel_environment_variables_loaded = False
def load_environment_variables(dir):
    """
    Loads important environment variables from the given kernel source tree.
    """
    global kernel_environment_variables_loaded
    if kernel_environment_variables_loaded:
        return

    log.info("Loading kernel environment variables for '{}'".format(dir))

    arch = detect_arch()
    log.info("Detected ARCH: {}".format(arch))
    set_env_default("ARCH", arch)
    set_env_default("SRCARCH", arch)
    set_env_default("CC", "gcc")
    set_env_default("HOSTCC", "gcc")
    set_env_default("HOSTCXX", "g++")

    os.environ["KERNELVERSION"] = subprocess.run(['make', 'kernelversion'], cwd=dir, stdout=subprocess.PIPE).stdout.decode().strip().splitlines()[0]
    os.environ["CC_VERSION_TEXT"] = subprocess.run(['gcc', '--version'], stdout=subprocess.PIPE).stdout.decode().strip().splitlines()[0]

    kernel_environment_variables_loaded = True

def load_kconfig(kernel_dir):
    kconfig_file = os.path.join(kernel_dir, "Kconfig")
    if not os.path.isfile(kconfig_file):
        raise Exception("'{}' must point to a valid Kconfig file!".format(kconfig_file))

    load_environment_variables(dir=kernel_dir)

    log.info("Loading '{}'".format(kconfig_file))
    os.environ['srctree'] = kernel_dir
    kconfig = kconfiglib.Kconfig(os.path.realpath(kconfig_file), warn_to_stderr=False)

    for w in kconfig.warnings:
        for line in w.split('\n'):
            log.verbose(line)

    allnoconfig(kconfig)
    return kconfig

def allnoconfig(kconfig):
    """
    Resets the current configuration to the equivalent of calling
    `make allnoconfig` in the kernel source tree.
    """

    log.info("Applying allnoconfig")

    # Allnoconfig from kconfiglib/allnoconfig.py
    warn_save = kconfig.warn
    kconfig.warn = False
    for sym in kconfig.unique_defined_syms:
        sym.set_value(YES if sym.is_allnoconfig_y else NO)
    kconfig.warn = warn_save
    kconfig.load_allconfig("allno.config")

class ExprSymbol:
    def __init__(self, sym):
        self.sym = sym

    def is_satisfied(self):
        return tri_to_bool(self.sym.tri_value)

class ExprCompare:
    def __init__(self, cmp_type, lhs, rhs):
        self.cmp_type = cmp_type
        self.lhs = lhs
        self.rhs = rhs

    def is_satisfied(self):
        if self.cmp_type == kconfiglib.EQUAL:
            return self.lhs == self.rhs
        elif self.cmp_type == kconfiglib.UNEQUAL:
            return self.lhs != self.rhs
        elif self.cmp_type == kconfiglib.LESS:
            return self.lhs < self.rhs
        elif self.cmp_type == kconfiglib.LESS_EQUAL:
            return self.lhs <= self.rhs
        elif self.cmp_type == kconfiglib.GREATER:
            return self.lhs > self.rhs
        elif self.cmp_type == kconfiglib.GREATER_EQUAL:
            return self.lhs >= self.rhs

    def __str__(self):
        return "{} {} {}".format(self.lhs.name, kconfiglib.REL_TO_STR[self.cmp_type], self.rhs.name)

class ExprIgnore:
    def is_satisfied(self):
        return False

class Expr:
    def __init__(self, expr):
        self.symbols = []
        self.expr_ignore_sym = None

        self.expr = self._parse(expr)

    def _add_symbol_if_nontrivial(self, sym):
        # If the symbol is aleady satisfied in the current config,
        # skip it.
        if sym.is_satisfied():
            return true

        # Return existing symbol if possible
        for s, sympy_s in self.symbols:
            if s.__class__ is sym.__class__ is ExprSymbol:
                if s.sym == sym.sym:
                    return sympy_s

        # Create new symbol
        i = len(self.symbols)
        s = Symbol(str(i))
        self.symbols.append((sym, s))
        return s

    def _parse(self, expr):
        def add_sym(expr):
            return self._add_symbol_if_nontrivial(ExprSymbol(expr))

        if expr.__class__ is not tuple:
            if expr.__class__ is kconfiglib.Symbol:
                if expr.is_constant:
                    return true if tri_to_bool(expr) else false
                elif expr.type in [kconfiglib.BOOL, kconfiglib.TRISTATE]:
                    return add_sym(expr)
                else:
                    # Ignore unknown symbol types
                    return self.expr_ignore()
            elif expr.__class__ is kconfiglib.Choice:
                return self.expr_ignore()
            else:
                raise Exception("Unexpected expression type '{}'".format(expr.__class__.__name__))
        else:
            # If the expression is an operator, resolve the operator.
            if expr[0] is kconfiglib.AND:
                return And(self._parse(expr[1]), self._parse(expr[2]))
            elif expr[0] is kconfiglib.OR:
                return Or(self._parse(expr[1]), self._parse(expr[2]))
            elif expr[0] is kconfiglib.NOT:
                return Not(self._parse(expr[1]))
            elif expr[0] is kconfiglib.EQUAL and expr[2].is_constant:
                if tri_to_bool(expr[2]):
                    return add_sym(expr[1])
                else:
                    return Not(add_sym(expr[1]))
            elif expr[0] in [kconfiglib.UNEQUAL, kconfiglib.LESS, kconfiglib.LESS_EQUAL, kconfiglib.GREATER, kconfiglib.GREATER_EQUAL]:
                if expr[1].__class__ is tuple or expr[2].__class__ is tuple:
                    raise Exception("Cannot compare expressions")
                return self._add_symbol_if_nontrivial(ExprCompare(expr[0], expr[1], expr[2]))
            else:
                raise Exception("Unknown expression type: '{}'".format(expr[0]))

    def expr_ignore(self):
        if not self.expr_ignore_sym:
            self.expr_ignore_sym = self._add_symbol_if_nontrivial(ExprIgnore())
        return self.expr_ignore_sym

    def simplify(self):
        self.expr = simplify_logic(self.expr)
        #self.expr = simplify_logic(to_cnf(self.expr), form='cnf')

    def unsatisfied_deps(self):
        configuration = satisfiable(self.expr)
        if not configuration:
            raise Exception("Cannot satisfy dependencies.")

        if configuration.get(True, False):
            return []

        deps = []
        for k in configuration:
            idx = int(k.name)
            deps.append((idx, self.symbols[idx][0], configuration[k]))

        deps.sort(key=lambda x: x[0], reverse=True)
        return deps

def required_deps(sym):
    expr = Expr(sym.direct_dep)
    expr.simplify()

    deps = []
    for k, s, v in expr.unsatisfied_deps():
        if s.__class__ is ExprIgnore:
            pass
        elif s.__class__ is ExprSymbol:
            deps.append((s.sym, v))
        else:
            raise Exception("Cannot automatically satisfy inequality: '{}'".format(s))
    return deps
