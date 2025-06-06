#!/usr/bin/env python3
import sys
import subprocess

import verible_verilog_syntax
from sympy import simplify

from typing import List, Optional, Dict

PREFIX = 'auto_ss'
RESET_SIGNALS = set(["rst", "nrst", "rstn", "n_rst", "rst_n", "reset", "nreset", "resetn", "n_reset", "reset_n"])

def find_path(root: verible_verilog_syntax.Node, tags: List[str]) -> Optional[verible_verilog_syntax.Node]:
    node = root

    for tag in tags:
        if node is None:
            return None
        node = node.find({"tag": tag})

    return node

def format_output(s: str) -> str:
    params = [
        "--port_declarations_alignment=align",
        "--named_port_alignment=align",
        "--assignment_statement_alignment=align",
        "--formal_parameters_alignment=align",
        "--module_net_variable_alignment=align",
        "--named_parameter_alignment=align",
        "--verify_convergence=false"
    ]
    proc = subprocess.run(["verible-verilog-format", "-"] + params,
        stdout=subprocess.PIPE,
        input=s,
        encoding="utf-8",
        check=True)
    return proc.stdout

def preprocess_inputs(paths: List[str]) -> str:
    res = []
    for p in paths:
        proc = subprocess.run(["verible-verilog-preprocessor", "preprocess", "+define+USE_AUTO_SS=1", p ],
            stdout=subprocess.PIPE,
            encoding="utf-8",
            check=True)
        res.append(proc.stdout)

    return '\n'.join(res)

def output_file(fp, node):
    begin = None
    s = ""
    for tok in verible_verilog_syntax.PreOrderTreeIterator(node):
        if isinstance(tok, InsertNode):
            s += f"\n{tok.text}\n"
        elif isinstance(tok, verible_verilog_syntax.TokenNode):
            if begin is None:
                begin = tok.start
            end = tok.end
            s += tok.syntax_data.source_code[begin:end].decode("utf-8")
            begin = end

    s += "\n\n\n"

    format = True
    if format:
        s = format_output(s)

    fp.write(s)


class InsertNode(verible_verilog_syntax.LeafNode):
    def __init__(self, contents: str, parent: verible_verilog_syntax.Node):
        super().__init__(parent)
        self.contents = contents

    @property
    def text(self) -> str:
        return self.contents

def add_text_before(node: verible_verilog_syntax.Node, text: str):
    parent = node.parent
    children = list(parent.children)
    idx = children.index(node)
    new_node = InsertNode(text, parent)
    children.insert(idx, new_node)
    parent.children = children


def add_text_after(node: verible_verilog_syntax.Node, text: str):
    parent = node.parent
    children = list(parent.children)
    idx = children.index(node)
    new_node = InsertNode(text, parent)
    children.insert(idx + 1, new_node)
    parent.children = children


class Dimension:
    def __init__(self, end, begin = None):
        self.end = simplify(end)
        self.begin = simplify(begin)
        if self.end == 0:
            e = self.end
            self.end = self.begin
            self.begin = e

        if begin:
            self.size = simplify(f"1+({self.end})-({self.begin})")
        else:
            self.size = self.end

    def __str__(self):
        if self.begin == self.end:
            return f"[{self.begin}]"
        else:
            return f"[{self.begin} +: {self.size}]"
            #return f"[{self.end}:{self.begin}]"

    def __eq__(self, o):
        return self.end == o.end and self.begin == o.begin

class Assignment:
    def __init__(self, always: verible_verilog_syntax.Node, syms: List[str]):
        self.syms = sorted(set(syms))
        self.registers = []
        self.always = always
        self.reset_signal = None
        self.reset_polarity = False

        for ev in always.iter_find_all({"tag": "kEventExpression"}):
            signal = ev.children[1].text
            if signal.lower() in RESET_SIGNALS:
                self.reset_signal = signal
                self.reset_polarity = ev.children[0].text == "posedge"

            
    def modify_tree(self):
        need_generate = False

        add_else = None

        if (self.reset_signal):
            # FIXME - we are assuming that the first if clause is going to be for reset
            cond = find_path(self.always, ["kIfClause"])
            if not self.reset_signal in cond.text:
                raise Exception(f"Reset without if {cond.text}")
            add_else = cond
            
        ctrl = find_path(self.always, ["kProceduralTimingControlStatement", "kEventControl"])
        

        wr_str = f"if ({PREFIX}_wr) begin\ninteger {PREFIX}_idx;\n"
        rd_str = ""
        for r in self.registers:
            if r.unpacked:
                len = r.unpacked.size
                need_generate = True

                block_name = f"blk_asg_{r.name}"

                dim = r.unpacked_dim(f"{PREFIX}_idx")
                
                wr_str += f"for ({PREFIX}_idx = 0; {PREFIX}_idx < ({len}); {PREFIX}_idx={PREFIX}_idx+1) begin\n"
                wr_str += f"{r.name}[{PREFIX}_idx] <= {PREFIX}_in{dim};\n"
                wr_str += "end\n"
                
                rd_str += f"for ({PREFIX}_idx = 0; {PREFIX}_idx < ({len}); {PREFIX}_idx={PREFIX}_idx+1) begin : {block_name}\n"
                rd_str += f"assign {PREFIX}_out{dim} = {r.name}[{PREFIX}_idx];\n"
                rd_str += "end\n"
            else:
                wr_str += f"{r.name} <= {PREFIX}_in{r.allocated};\n"
                rd_str += f"assign {PREFIX}_out{r.allocated} = {r.name};\n"
        wr_str += "end"

        if need_generate:
            rd_str = "generate\n" + rd_str + "\nendgenerate\n"

        if add_else:
            add_text_after(add_else, "else " + wr_str)
        else:
            add_text_after(ctrl, "begin")
            add_text_after(ctrl.parent.children[-1], wr_str + "\nend\n")

        add_text_after(self.always, rd_str)


    def __repr__(self) -> str:
        return self.name

class Register:
    def __init__(self, name, packed: Optional[Dimension] = None, unpacked: Optional[Dimension] = None):
        self.name = name
        self.packed = packed
        self.unpacked = unpacked

        self.allocated = None

    def size(self):
        if self.packed and self.unpacked: return f"({self.packed.size})*({self.unpacked.size})"
        elif self.packed:
            return self.packed.size
        elif self.unpacked:
            return self.unpacked.size
        else:
            return "1"

    def __eq__(self, o) -> bool:
        return self.name == o.name and self.packed == o.packed and self.unpacked == o.unpacked

    def allocate(self, offset):
        self.allocated = Dimension(f"({offset})+({self.size()})-1", offset)
        return f"({offset})+({self.size()})"

    def unpacked_dim(self, index: str) -> Dimension:
        base = self.allocated.begin
        return Dimension(
            f"(({self.packed.size}) * (({index}) + 1)) + ({base}) - 1",
            f"(({self.packed.size}) * ({index})) + ({base})")

    def __repr__(self) -> str:
        p = ""
        u = ""
        if self.packed:
            p = str(self.packed)
        if self.unpacked:
            u = str(self.unpacked)

        return f"reg {p} {self.name} {u}"

class ModuleInstance:
    def __init__(self, node: verible_verilog_syntax.Node, name, module_name):
        self.name = name
        self.module_name = module_name
        self.module = None
        self.node = node
        self.params = []
        self.named_params = {}
        self.allocated = None

    def add_param(self, name, value):
        if name is None:
            if len(self.named_params):
                raise Exception("Adding positional parameter when named parameters already exist")
            self.params.append(value)
        else:
            if len(self.params):
                raise Exception("Adding named parameter when positional parameters already exist")
            self.named_params[name] = value

    def allocate(self, offset):
        module_dim = self.module.allocate()
        if module_dim is None:
            return offset

        params = self.module.eval_params(self.params, self.named_params)
        end = str(module_dim.end)
        for k, v in params.items():
            end = end.replace(k, f"({v})")

        self.allocated = Dimension(f"({end})+({offset})", offset)
        return f"({offset})+({self.size()})"
    
    def size(self):
        if self.allocated:
            return self.allocated.size
        else:
            return "0"

    def modify_tree(self):
        if not self.allocated:
            return
        port_list = find_path(self.node, ["kGateInstance", "kPortActualList"])
        add_text_after(port_list, f""",
                .{PREFIX}_in({PREFIX}_in{self.allocated}),
                .{PREFIX}_out({PREFIX}_out{self.allocated}),
                .{PREFIX}_wr({PREFIX}_wr)""")

    def __repr__(self):
        return f"ModuleInstance: {self.module_name} {self.name} {self.params} {self.named_params}"

class Module:
    def __init__(self, node: verible_verilog_syntax.Node):
        self.node = node
        name = find_path(node, ["kModuleHeader", "SymbolIdentifier"])
        self.name = name.text
        self.instances = []
        self.registers = []
        self.assignments = []
        self.parameters = []
        self.state_dim = None

        self.allocated = False
        self.predefined = False

        self.extract_module_instances()
        self.extract_registers()
        self.extract_assignments()
        self.extract_parameters()

    def __repr__(self) -> str:
        return f"{self.name}\nParameters: {self.parameters}\nInstances: {self.instances}\nRegisters: {self.registers}\nAssignments: {self.assignments}"

    def eval_params(self, positional: List[str], named: Dict[str,str]) -> Dict[str,str]:
        r = {}
        for idx, x in enumerate(self.parameters):
            if idx < len(positional):
                r[x[0]] = positional[idx]
            elif x[0] in named:
                r[x[0]] = named[x[0]]
            else:
                r[x[0]] = x[1]
        return r


    def extract_module_instances(self):
        for decl in self.node.iter_find_all({"tag": "kDataDeclaration"}):
            data_type = find_path(decl, ["kInstantiationType", "kDataType"])
            ports = find_path(decl, ["kPortActualList"])
            if data_type is None or ports is None:
                continue
            instance_def = find_path(decl, ["kGateInstance"])
            ref = find_path(data_type, ["kUnqualifiedId", "SymbolIdentifier"])
            instance = ModuleInstance(decl, instance_def.children[0].text, ref.text)

            for param in data_type.iter_find_all({"tag": "kParamByName"}):
                param_name = param.children[1].text
                param_value = param.children[2].children[1].text
                instance.add_param(param_name, param_value)
            
            params = find_path(data_type, ["kActualParameterPositionalList"])
            if params:
                for param in params.children[::2]:
                    instance.add_param(None, param.text)

            self.instances.append(instance)

    def extract_registers(self):
        dup_track = {}
        for decl in self.node.iter_find_all({"tag": "kDataDeclaration"}):
            dims = find_path(decl, ["kPackedDimensions", "kDimensionRange"])
            packed = None
            if dims:
                packed = Dimension(dims.children[1].text, dims.children[3].text)

            instances = decl.find({"tag": "kGateInstanceRegisterVariableList"})
            for variable in instances.iter_find_all({"tag": "kRegisterVariable"}):
                unpacked = None
                dims = find_path(decl, ["kUnpackedDimensions", "kDimensionRange"])
                if dims:
                    unpacked = Dimension(dims.children[1].text, dims.children[3].text)

                sym = variable.find({"tag": "SymbolIdentifier"})
                reg = Register(sym.text, packed=packed, unpacked=unpacked)
                if reg.name in dup_track:
                    if dup_track[reg.name] != reg:
                        raise Exception(f"Conflicting register declarations: {reg} {dup_track[reg.name]}")
                else:
                    dup_track[reg.name] = reg
                    self.registers.append(reg)

        for decl in self.node.iter_find_all({"tag": ["kPortDeclaration", "kModulePortDeclaration"]}):
            packed = None
            dims = find_path(decl, ["kPackedDimensions", "kDimensionRange"])
            if dims:
                packed = Dimension(dims.children[1].text, dims.children[3].text)

            sym = find_path(decl, ["kUnqualifiedId"])
            if sym is None:
                sym = find_path(decl, ["kIdentifierUnpackedDimensions", "SymbolIdentifier"])
            reg = Register(sym.text, packed=packed, unpacked=None)
            if reg.name in dup_track:
                if dup_track[reg.name] != reg:
                    raise Exception(f"Conflicting register declarations: {reg} {dup_track[reg.name]}")
            else:
                dup_track[reg.name] = reg
                self.registers.append(reg)

    def extract_assignments(self):
        for always in self.node.iter_find_all({"tag": "kAlwaysStatement"}):
            syms = []
            for assign in always.iter_find_all({"tag": "kNonblockingAssignmentStatement"}):
                target = assign.find({"tag": "kLPValue"})
                if not target:
                    print("WARN: assignment without target")
                    continue
                sym = target.find({"tag": "SymbolIdentifier"})
                if not sym:
                    print("WARN: assignment without symbol")
                syms.append(sym.text)
            if len(syms):
                self.assignments.append(Assignment(always, syms))    

    def extract_parameters(self):
        for decl in self.node.iter_find_all({"tag": "kParamDeclaration"}):
            name = find_path(decl, ["kParamType", "SymbolIdentifier"])
            value = find_path(decl, ["kTrailingAssign", "kExpression"])
            self.parameters.append((name.text, value.text))

        for decl in self.node.iter_find_all({"tag": "kParameterAssign"}):
            self.parameters.append((decl.children[0].text, decl.children[2].text))

    def allocate(self) -> Optional[Dimension]:
        if self.allocated:
            return self.state_dim

        for reg in self.registers:
            if reg.name == f"{PREFIX}_out":
                self.predefined = True
                self.assignments = []
                self.registers = []
                reg.allocate("0")
                self.state_dim = reg.allocated
                self.allocated = True
                return self.state_dim
                
        assigned = {}
        for a in self.assignments:
            for sym in a.syms:
                assigned[sym] = 1

        allocated = {}
        offset = "0"
        for reg in self.registers:
            if reg.name in assigned:
                offset = reg.allocate(offset)
                allocated[reg.name] = reg
        self.registers = allocated.values()

        for a in self.assignments:
            for sym in a.syms:
                a.registers.append(allocated[sym])

        for inst in self.instances:
            offset = inst.allocate(offset)
        
        self.allocated = True
        if offset != "0":
            self.state_dim = Dimension(f"({offset})-1", "0")
            return self.state_dim
        else:
            return None



    def print_allocation(self):
        if not self.state_dim:
            return

        print(self.name)
        print(f"  input [{self.state_dim.end}:{self.state_dim.begin}] state_in,")
        print("  input state_wr,")
        print(f"  output [{self.state_dim.end}:{self.state_dim.begin}] state_out,")

        for r in self.registers:
            print(f"    assign state_out{r.allocated} = {r.name};")
            print(f"    if (state_wr) {r.name} <= state_in{r.allocated};")

        for i in self.instances:
            print(f"    {i.module_name} {i.name} (")
            print(f"        .state_in(state_in{i.allocated}),")
            print(f"        .state_out(state_out{i.allocated}),")
            print(f"        .state_wr(state_wr),")

    def modify_tree(self):
        if not self.state_dim:
            return

        if self.predefined:
            return

        verilog1995 = find_path(self.node, ["kModuleItemList", "kModulePortDeclaration"]) is not None
        
        port_decl = find_path(self.node, ["kModuleHeader", "kPortDeclarationList"])
        header = find_path(self.node, ["kModuleHeader"])

        if verilog1995:
            add_text_after(port_decl, f",\n{PREFIX}_in, {PREFIX}_wr, {PREFIX}_out")
            s =  f"input [{self.state_dim.end}:{self.state_dim.begin}] {PREFIX}_in;\n"
            s += f"input {PREFIX}_wr;\n"
            s += f"output [{self.state_dim.end}:{self.state_dim.begin}] {PREFIX}_out;\n"
            add_text_after(header, s)

        else:
            s = f",\ninput [{self.state_dim.end}:{self.state_dim.begin}] {PREFIX}_in, input {PREFIX}_wr, "
            s += f"output [{self.state_dim.end}:{self.state_dim.begin}] {PREFIX}_out"
            add_text_after(port_decl, s)

        add_text_after(header, f"genvar {PREFIX}_idx;") # used by assignments

        for i in self.instances:
            i.modify_tree()

        for a in self.assignments:
            a.modify_tree()


    def output_module(self, fp):
        s = "///////////////////////////////////////////\n"
        s += f"// MODULE {self.name}\n"
        begin = None
        for tok in verible_verilog_syntax.PreOrderTreeIterator(self.node):
            if isinstance(tok, InsertNode):
                s += f"\n{tok.text}\n"
            elif isinstance(tok, verible_verilog_syntax.TokenNode):
                if begin is None:
                    begin = tok.start
                end = tok.end
                s += tok.syntax_data.source_code[begin:end].decode("utf-8")
                begin = end

        s += "\n\n\n"

        format = True
        if format:
            s = format_output(s)
  
        fp.write(s)



    def post_order(self):
        r = []
        for inst in self.instances:
            r.extend(inst.module.post_order())
        r.append(self)
        return r

def resolve_modules(root: str, modules: List[Module]) -> Module:
    name_map = {}
    for m in modules:
        name_map[m.name] = m

    for m in modules:
        for inst in m.instances:
            inst.module = name_map[inst.module_name]

    return name_map[root]


def process_file_data(data: verible_verilog_syntax.SyntaxData) -> List[Module]:
    if not data.tree:
        return

    modules = []
    for module_node in data.tree.iter_find_all({"tag": "kModuleDeclaration"}):
        modules.append(Module(module_node)) 

    return modules

def main():
    if len(sys.argv) < 4:
        print(f"Usage: {sys.argv[0]} " +
                    "MODULE_NAME OUTPUT_FILE VERILOG_FILE [VERILOG_FILE [...]]")
        return 1

    root_module_name = sys.argv[1]
    output_name = sys.argv[2]
    files = sys.argv[3:]

    out_fp = None
    if output_name == '-':
        out_fp = sys.stdout
    else:
        out_fp = open(output_name, "wt")

    parser = verible_verilog_syntax.VeribleVerilogSyntax(executable="verible-verilog-syntax")
    preprocessed = preprocess_inputs(files)
    data = parser.parse_string(preprocessed)
    modules = process_file_data(data)

    root_module = resolve_modules(root_module_name, modules)

    root_module.allocate()

    output_modules = root_module.post_order()
    visited = set()
    for module in output_modules:
        if module in visited:
            continue
        module.modify_tree()
        #module.print_allocation()
        module.output_module(out_fp)
        visited.add(module)

    if out_fp != sys.stdout:
        out_fp.close()

if __name__ == "__main__":
    sys.exit(main())
