"""Static check: every frappe.* name / keyword argument this app uses must exist in the
frappe version that is actually installed on the site.

Usage (no bench needed):
    git clone --depth 1 --branch v15.120.1 https://github.com/frappe/frappe.git /tmp/frappe_src
    python scripts/check_frappe_api.py /tmp/frappe_src

Exit code 1 and a list of problems when something does not resolve. Run it before every
push; the site version is in the "App Versions" block of any error report.
"""

import ast
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "trident_attendance"
KNOWN_ATTRS = {"db", "utils", "local", "session", "flags", "form_dict", "model", "qb", "cache", "logger", "permissions", "json"}
DOC_METHODS = {"insert", "save", "submit", "cancel", "add_comment", "update", "get", "set", "has_value_changed", "is_new", "run_method", "get_doc_before_save", "db_set", "reload", "delete", "append"}


def defs_in(path):
	out = {}
	tree = ast.parse(path.read_text(encoding="utf-8"))
	for node in ast.walk(tree):
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
			a = node.args
			names = [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs]
			out.setdefault(node.name, (names, a.kwarg is not None))
		elif isinstance(node, ast.ClassDef):
			out.setdefault(node.name, ([], True))
	return out


def names_in(path):
	names = set()
	tree = ast.parse(path.read_text(encoding="utf-8"))
	for node in tree.body:
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
			names.add(node.name)
		elif isinstance(node, ast.Assign):
			for t in node.targets:
				for n in ast.walk(t):
					if isinstance(n, ast.Name):
						names.add(n.id)
		elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
			names.add(node.target.id)
		elif isinstance(node, (ast.Import, ast.ImportFrom)):
			for alias in node.names:
				names.add((alias.asname or alias.name).split(".")[0])
	return names


def main(frappe_root: Path) -> int:
	fr = frappe_root / "frappe"
	if not (fr / "__init__.py").exists():
		print(f"frappe package not found under {frappe_root}")
		return 2

	frappe_init = defs_in(fr / "__init__.py")
	frappe_names = names_in(fr / "__init__.py") | names_in(fr / "exceptions.py")
	db_defs = defs_in(fr / "database" / "database.py")
	db_defs.update({k: v for k, v in defs_in(fr / "database" / "mariadb" / "database.py").items() if k not in db_defs})
	query_execute = defs_in(fr / "model" / "db_query.py")["execute"]
	doc_defs = defs_in(fr / "model" / "document.py")
	doc_defs.update({k: v for k, v in defs_in(fr / "model" / "base_document.py").items() if k not in doc_defs})
	utils_names = names_in(fr / "utils" / "__init__.py") | names_in(fr / "utils" / "data.py")
	module_files = {
		"frappe.utils": [fr / "utils" / "__init__.py", fr / "utils" / "data.py"],
		"frappe.utils.synchronization": [fr / "utils" / "synchronization.py"],
		"frappe.utils.scheduler": [fr / "utils" / "scheduler.py"],
		"frappe.model.document": [fr / "model" / "document.py"],
		"frappe.permissions": [fr / "permissions.py"],
		"frappe": [fr / "__init__.py", fr / "exceptions.py"],
	}

	problems = []

	def check_kwargs(where, fn, kwargs, sig):
		params, has_star = sig
		for kw in kwargs:
			if kw.arg is not None and kw.arg not in params and not has_star:
				problems.append(f"{where}: {fn}() has no keyword '{kw.arg}' (params: {', '.join(params)})")

	for py in sorted(APP.rglob("*.py")):
		tree = ast.parse(py.read_text(encoding="utf-8"))
		rel = py.relative_to(APP.parent)
		for node in ast.walk(tree):
			where = f"{rel}:{getattr(node, 'lineno', '?')}"
			if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("frappe"):
				if node.module in module_files:
					avail = set()
					for f in module_files[node.module]:
						avail |= names_in(f)
					for alias in node.names:
						if alias.name not in avail:
							problems.append(f"{where}: '{alias.name}' not found in {node.module}")
				else:
					mod = frappe_root / Path(*node.module.split("."))
					if not mod.with_suffix(".py").exists() and not (mod / "__init__.py").exists():
						problems.append(f"{where}: module {node.module} not found")
			if isinstance(node, ast.Call):
				f = node.func
				if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Attribute) and isinstance(f.value.value, ast.Name) and f.value.value.id == "frappe":
					if f.value.attr == "db":
						if f.attr not in db_defs:
							problems.append(f"{where}: frappe.db.{f.attr} does not exist")
						else:
							check_kwargs(where, f"frappe.db.{f.attr}", node.keywords, db_defs[f.attr])
					elif f.value.attr == "utils" and f.attr not in utils_names:
						problems.append(f"{where}: frappe.utils.{f.attr} does not exist")
				elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "frappe":
					if f.attr in frappe_init:
						sig = frappe_init[f.attr]
						if f.attr in ("get_all", "get_list"):
							sig = (query_execute[0] + ["doctype"], query_execute[1])
						check_kwargs(where, f"frappe.{f.attr}", node.keywords, sig)
					elif f.attr not in frappe_names and f.attr not in KNOWN_ATTRS:
						problems.append(f"{where}: frappe.{f.attr} does not exist")
				elif isinstance(f, ast.Attribute) and f.attr in DOC_METHODS and f.attr in doc_defs and node.keywords:
					check_kwargs(where, f"Document.{f.attr}", node.keywords, doc_defs[f.attr])
			if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "frappe":
				if node.attr not in frappe_names and node.attr not in frappe_init and node.attr not in KNOWN_ATTRS:
					problems.append(f"{where}: attribute frappe.{node.attr} not found")

	seen, out = set(), []
	for p in problems:
		if p not in seen:
			seen.add(p)
			out.append(p)
	print("\n".join(out) if out else "ALL FRAPPE API USAGES RESOLVE")
	return 1 if out else 0


if __name__ == "__main__":
	if len(sys.argv) < 2:
		print(__doc__)
		sys.exit(2)
	sys.exit(main(Path(sys.argv[1]).resolve()))
