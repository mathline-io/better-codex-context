if __package__:
    from .cli import main
else:
    import os
    import sys
    package_directory = os.path.dirname(os.path.realpath(__file__))
    # Direct execution puts this directory first on sys.path, where a module of ours could shadow a stdlib one.
    sys.path[:] = [entry for entry in sys.path if os.path.realpath(entry or os.getcwd()) != package_directory]
    sys.path.insert(0, os.path.dirname(package_directory))
    from better_codex_context.cli import main

raise SystemExit(main())
