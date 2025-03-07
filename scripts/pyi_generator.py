"""The pyi generator module."""

import importlib
import inspect
import os
import re
import sys
from inspect import getfullargspec
from pathlib import Path
from typing import Any, Dict, List, Optional, get_args

import black

from reflex.components.component import Component
from reflex.vars import Var

ruff_dont_remove = [Var, Optional, Dict, List]

EXCLUDED_FILES = [
    "__init__.py",
    "component.py",
    "bare.py",
    "foreach.py",
    "cond.py",
    "multiselect.py",
]

DEFAULT_TYPING_IMPORTS = {"overload", "Optional", "Union"}


def _get_type_hint(value, top_level=True, no_union=False):
    res = ""
    args = get_args(value)
    if args:
        res = f"{value.__name__}[{', '.join([_get_type_hint(arg, top_level=False) for arg in args if arg is not type(None)])}]"

        if value.__name__ == "Var":
            types = [res] + [
                _get_type_hint(arg, top_level=False)
                for arg in args
                if arg is not type(None)
            ]
            if len(types) > 1 and not no_union:
                res = ", ".join(types)
                res = f"Union[{res}]"
    elif isinstance(value, str):
        ev = eval(value)
        res = _get_type_hint(ev, top_level=False) if ev.__name__ == "Var" else value
    else:
        res = value.__name__
    if top_level and not res.startswith("Optional"):
        res = f"Optional[{res}]"
    return res


def _get_typing_import(_module):
    src = [
        line
        for line in inspect.getsource(_module).split("\n")
        if line.startswith("from typing")
    ]
    if len(src):
        return set(src[0].rpartition("from typing import ")[-1].split(", "))
    return set()


def _get_var_definition(_module, _var_name):
    return [
        line.split(" = ")[0]
        for line in inspect.getsource(_module).splitlines()
        if line.startswith(_var_name)
    ]


class PyiGenerator:
    """A .pyi file generator that will scan all defined Component in Reflex and
    generate the approriate stub.
    """

    modules: list = []
    root: str = ""
    current_module: Any = {}
    default_typing_imports: set = DEFAULT_TYPING_IMPORTS

    def _generate_imports(self, variables, classes):
        variables_imports = {
            type(_var) for _, _var in variables if isinstance(_var, Component)
        }
        bases = {
            base
            for _, _class in classes
            for base in _class.__bases__
            if inspect.getmodule(base) != self.current_module
        } | variables_imports
        bases.add(Component)
        typing_imports = self.default_typing_imports | _get_typing_import(
            self.current_module
        )
        bases = sorted(bases, key=lambda base: base.__name__)
        return [
            f"from typing import {','.join(sorted(typing_imports))}",
            *[f"from {base.__module__} import {base.__name__}" for base in bases],
            "from reflex.vars import Var, BaseVar, ComputedVar",
            "from reflex.event import EventHandler, EventChain, EventSpec",
        ]

    def _generate_pyi_class(self, _class: type[Component]):
        create_spec = getfullargspec(_class.create)
        lines = [
            "",
            f"class {_class.__name__}({', '.join([base.__name__ for base in _class.__bases__])}):",
        ]
        definition = f"    @overload\n    @classmethod\n    def create(cls, *children, "

        for kwarg in create_spec.kwonlyargs:
            if kwarg in create_spec.annotations:
                definition += f"{kwarg}: {_get_type_hint(create_spec.annotations[kwarg])} = None, "
            else:
                definition += f"{kwarg}, "

        for name, value in _class.__annotations__.items():
            if name in create_spec.kwonlyargs:
                continue
            definition += f"{name}: {_get_type_hint(value)} = None, "

        for trigger in sorted(_class().get_triggers()):
            definition += f"{trigger}: Optional[Union[EventHandler, EventSpec, List, function, BaseVar]] = None, "

        definition = definition.rstrip(", ")
        definition += f", **props) -> '{_class.__name__}': # type: ignore\n"

        definition += self._generate_docstrings(_class, _class.__annotations__.keys())
        lines.append(definition)
        lines.append("        ...")
        return lines

    def _generate_docstrings(self, _class, _props):
        props_comments = {}
        comments = []
        for _i, line in enumerate(inspect.getsource(_class).splitlines()):
            reached_functions = re.search("def ", line)
            if reached_functions:
                # We've reached the functions, so stop.
                break

            # Get comments for prop
            if line.strip().startswith("#"):
                comments.append(line)
                continue

            # Check if this line has a prop.
            match = re.search("\\w+:", line)
            if match is None:
                # This line doesn't have a var, so continue.
                continue

            # Get the prop.
            prop = match.group(0).strip(":")
            if prop in _props:
                # This isn't a prop, so continue.
                props_comments[prop] = "\n".join(
                    [comment.strip().strip("#") for comment in comments]
                )
                comments.clear()
                continue
        new_docstring = []
        for i, line in enumerate(_class.create.__doc__.splitlines()):
            if i == 0:
                new_docstring.append(" " * 8 + '"""' + line)
            else:
                new_docstring.append(line)
            if "*children" in line:
                for nline in [
                    f"{line.split('*')[0]}{n}:{c}" for n, c in props_comments.items()
                ]:
                    new_docstring.append(nline)
        new_docstring += ['"""']
        return "\n".join(new_docstring)

    def _generate_pyi_variable(self, _name, _var):
        return _get_var_definition(self.current_module, _name)

    def _generate_function(self, _name, _func):
        definition = "".join(inspect.getsource(_func).split(":\n")[0].split("\n"))
        return [f"{definition}:", "    ..."]

    def _write_pyi_file(self, variables, functions, classes):
        pyi_content = [
            f'"""Stub file for {self.current_module_path}.py"""',
            "# ------------------- DO NOT EDIT ----------------------",
            "# This file was generated by `scripts/pyi_generator.py`!",
            "# ------------------------------------------------------",
            "",
        ]
        pyi_content.extend(self._generate_imports(variables, classes))

        for _name, _var in variables:
            pyi_content.extend(self._generate_pyi_variable(_name, _var))

        for _fname, _func in functions:
            pyi_content.extend(self._generate_function(_fname, _func))

        for _, _class in classes:
            pyi_content.extend(self._generate_pyi_class(_class))

        pyi_filename = f"{self.current_module_path}.pyi"
        pyi_path = os.path.join(self.root, pyi_filename)

        with open(pyi_path, "w") as pyi_file:
            pyi_file.write("\n".join(pyi_content))
        black.format_file_in_place(
            src=Path(pyi_path),
            fast=True,
            mode=black.FileMode(),
            write_back=black.WriteBack.YES,
        )

    def _scan_file(self, file):
        self.current_module_path = os.path.splitext(file)[0]
        module_import = os.path.splitext(os.path.join(self.root, file))[0].replace(
            "/", "."
        )

        self.current_module = importlib.import_module(module_import)

        local_variables = [
            (name, obj)
            for name, obj in vars(self.current_module).items()
            if not name.startswith("__")
            and not inspect.isclass(obj)
            and not inspect.isfunction(obj)
        ]

        functions = [
            (name, obj)
            for name, obj in vars(self.current_module).items()
            if not name.startswith("__")
            and (
                not inspect.getmodule(obj)
                or inspect.getmodule(obj) == self.current_module
            )
            and inspect.isfunction(obj)
        ]

        class_names = [
            (name, obj)
            for name, obj in vars(self.current_module).items()
            if inspect.isclass(obj)
            and issubclass(obj, Component)
            and obj != Component
            and inspect.getmodule(obj) == self.current_module
        ]
        if not class_names:
            return
        print(f"Parsed {file}: Found {[n for n,_ in class_names]}")
        self._write_pyi_file(local_variables, functions, class_names)

    def _scan_folder(self, folder):
        for root, _, files in os.walk(folder):
            self.root = root
            for file in files:
                if file in EXCLUDED_FILES:
                    continue
                if file.endswith(".py"):
                    self._scan_file(file)

    def scan_all(self, targets):
        """Scan all targets for class inheriting Component and generate the .pyi files.

        Args:
            targets: the list of file/folders to scan.
        """
        for target in targets:
            if target.endswith(".py"):
                self.root, _, file = target.rpartition("/")
                self._scan_file(file)
            else:
                self._scan_folder(target)


if __name__ == "__main__":
    targets = sys.argv[1:] if len(sys.argv) > 1 else ["reflex/components"]
    print(f"Running .pyi generator for {targets}")
    gen = PyiGenerator()
    gen.scan_all(targets)
