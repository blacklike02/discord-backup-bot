"""Windows executable entry point; keep verification independent of bot startup."""
import sys

if len(sys.argv) > 1 and sys.argv[1] == '--verify':
    del sys.argv[1]
    from verify_backup import main
    raise SystemExit(main())

from bot import main
main()
