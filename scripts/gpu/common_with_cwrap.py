# this code should be common among cwrap and ATen preprocessing
# for now, I have put it in one place but right now is copied out of cwrap

import copy


def parse_arguments(args):
    new_args = []
    for arg in args:
        # Simple arg declaration of form "<type> <name>"
        if isinstance(arg, str):
            t, _, name = arg.partition(" ")
            new_args.append({"type": t, "name": name})
        elif isinstance(arg, dict):
            if "arg" in arg:
                arg["type"], _, arg["name"] = arg["arg"].partition(" ")
                del arg["arg"]
            new_args.append(arg)
        else:
            raise AssertionError()
    return new_args


def set_declaration_defaults(declaration):
    if "options" not in declaration:
        declaration["options"] = [
            {
                "arguments": copy.deepcopy(declaration["arguments"]),
                "schema_order_arguments": copy.deepcopy(
                    declaration["schema_order_arguments"]
                ),
            }
        ]
        del declaration["arguments"]
        del declaration["schema_order_arguments"]
    # Parse arguments (some of them can be strings)
    for option in declaration["options"]:
        option["arguments"] = parse_arguments(option["arguments"])
        option["schema_order_arguments"] = parse_arguments(
            option["schema_order_arguments"]
        )
    # Propagate defaults from declaration to options
    for option in declaration["options"]:
        for k, v in declaration.items():
            # TODO(zach): why does cwrap not propagate 'name'? I need it
            # propagaged for ATen
            if k != "options":
                option.setdefault(k, v)


# TODO(zach): added option to remove keyword handling for C++ which cannot
# support it.


def filter_unique_options(options, allow_kwarg, type_to_signature, remove_self):
    def exclude_arg(arg):
        return arg["type"] == "CONSTANT"

    def exclude_arg_with_self_check(arg):
        return exclude_arg(arg) or (remove_self and arg["name"] == "self")

    def signature(option, kwarg_only_count):
        if kwarg_only_count == 0:
            kwarg_only_count = None
        else:
            kwarg_only_count = -kwarg_only_count
        arg_signature = "#".join(
            type_to_signature.get(arg["type"], arg["type"])
            for arg in option["arguments"][:kwarg_only_count]
            if not exclude_arg_with_self_check(arg)
        )
        if kwarg_only_count is None:
            return arg_signature
        kwarg_only_signature = "#".join(
            arg["name"] + "#" + arg["type"]
            for arg in option["arguments"][kwarg_only_count:]
            if not exclude_arg(arg)
        )
        return arg_signature + "#-#" + kwarg_only_signature

    seen_signatures = set()
    unique = []
    for option in options:
        # if only check num_kwarg_only == 0 if allow_kwarg == False
        limit = len(option["arguments"]) if allow_kwarg else 0
        for num_kwarg_only in range(0, limit + 1):
            sig = signature(option, num_kwarg_only)
            if sig not in seen_signatures:
                if num_kwarg_only > 0:
                    for arg in option["arguments"][-num_kwarg_only:]:
                        arg["kwarg_only"] = True
                unique.append(option)
                seen_signatures.add(sig)
                break
    return unique


def sort_by_number_of_args(declaration, reverse=True):
    def num_args(option):
        return len(option["arguments"])

    declaration["options"].sort(key=num_args, reverse=reverse)


class Function(object):
    def __init__(self, name):
        self.name = name
        self.arguments = []

    def add_argument(self, arg):
        assert isinstance(arg, Argument)
        self.arguments.append(arg)

    def __repr__(self):
        return (
            self.name
            + "("
            + ", ".join(map(lambda a: a.__repr__(), self.arguments))
            + ")"
        )


class Argument(object):
    def __init__(self, _type, name, is_optional):
        self.type = _type
        self.name = name
        self.is_optional = is_optional

    def __repr__(self):
        return self.type + " " + self.name


def parse_header(path):
    with open(path, "r") as f:
        lines = f.read().split("\n")

    # Remove empty lines and prebackend directives
    lines = filter(lambda l: l and not l.startswith("#"), lines)
    # Remove line comments
    lines = map(lambda l: l.partition("//"), lines)
    # Select line and comment part
    lines = map(lambda l: (l[0].strip(), l[2].strip()), lines)
    # Remove trailing special signs
    lines = map(lambda l: (l[0].rstrip(");").rstrip(","), l[1]), lines)
    # Split arguments
    lines = map(lambda l: (l[0].split(","), l[1]), lines)
    # Flatten lines
    new_lines = []
    for l, c in lines:
        for split in l:
            new_lines.append((split, c))
    lines = new_lines
    del new_lines
    # Remove unnecessary whitespace
    lines = map(lambda l: (l[0].strip(), l[1]), lines)
    # Remove empty lines
    lines = filter(lambda l: l[0], lines)
    generic_functions = []
    for l, c in lines:
        if l.startswith("TH_API void THNN_"):
            fn_name = l[len("TH_API void THNN_") :]
            if fn_name[0] == "(" and fn_name[-2] == ")":
                fn_name = fn_name[1:-2]
            else:
                fn_name = fn_name[:-1]
            generic_functions.append(Function(fn_name))
        elif l.startswith("THC_API void THNN_"):
            fn_name = l[len("THC_API void THNN_") :]
            if fn_name[0] == "(" and fn_name[-2] == ")":
                fn_name = fn_name[1:-2]
            else:
                fn_name = fn_name[:-1]
            generic_functions.append(Function(fn_name))
        elif l:
            t, name = l.split()
            if "*" in name:
                t = t + "*"
                name = name[1:]
            generic_functions[-1].add_argument(Argument(t, name, "[OPTIONAL]" in c))
    return generic_functions
