"""Runs the program as ``python -m hello``, delegating to the greeter.

It exists so the module form behaves as the ``hello`` console script.
"""

import sys

from hello.greeting.greeter import main

sys.exit(main())
