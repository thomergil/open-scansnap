import sys

if "--self-test" in sys.argv:
    from openscansnap.selftest import main
else:
    from openscansnap.menubar import main

main()
