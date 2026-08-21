import sys, traceback, datetime
sys.path.insert(0, ".")
# 注入参数：proxy + extended + 2400
sys.argv = ["_xsec.py", "--money", "proxy", "--universe", "extended", "--days", "2400"]
import _xsec
try:
    _xsec.main()
    with open("_runB_err.txt", "w") as f:
        f.write("MAIN_OK at %s\n" % datetime.datetime.now())
except SystemExit as e:
    with open("_runB_err.txt", "w") as f:
        f.write("SYSTEM_EXIT code=%s at %s\n" % (e.code, datetime.datetime.now()))
        traceback.print_exc(file=f)
except Exception as e:
    with open("_runB_err.txt", "w") as f:
        f.write("EXCEPTION at %s\n" % datetime.datetime.now())
        traceback.print_exc(file=f)
