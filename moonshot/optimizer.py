"""Shim: phased daily optimization lives in ``moonshot.daily_optimizer``.

``python -m moonshot.optimizer`` is equivalent to ``python -m moonshot.daily_optimizer``.
For rolling (R24), use ``python -m moonshot.rolling_optimizer``.
"""

from __future__ import annotations

from moonshot.daily_optimizer import main

if __name__ == "__main__":
    main()
