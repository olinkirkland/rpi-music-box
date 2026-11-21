"""Every method the page's templates call must exist in the JS (catches a
deleted or misspelled handler, which compiles fine but fails on click)."""
import re
import sys

html = open(sys.argv[1]).read()
js = "".join(open(f).read() for f in sys.argv[2:])
tpl = html + "".join(re.findall(r"template:\s*`([\s\S]*?)`", js))
calls = set(re.findall(r'@[\w.:-]+="([A-Za-z_]\w*)\(', tpl)) | set(re.findall(r'@[\w.:-]+="([A-Za-z_]\w*)"', tpl))
calls |= set(re.findall(r"\{\{[^}]*?\b([a-z]\w*)\(", tpl)) | set(re.findall(r':[\w-]+="([a-z]\w*)\(', tpl))
defined = set(re.findall(r"^\s{4}(?:async\s+)?([A-Za-z_]\w*)\(", js, re.M))
builtins = {"encodeURIComponent", "Number", "Math", "String", "filter", "join", "indexOf", "trim"}
missing = sorted(calls - defined - builtins)
print("handlers: " + (", ".join(missing) + " not defined" if missing else "all defined"))
sys.exit(1 if missing else 0)
