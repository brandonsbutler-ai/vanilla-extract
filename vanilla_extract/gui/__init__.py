"""The desktop application.

Split in two on purpose:

    session.py   what the window decides -- standard library only, no toolkit,
                 driven by tests with no display attached
    qt_app.py    the window itself, and the project's only dependency

The library's claim is that it has none, and that stays true: nothing outside
this package imports either module, and the command line never touches them.
"""
