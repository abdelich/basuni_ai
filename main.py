"""
Точка входа оркестратора BasuniAI.
Запуск: из корня проекта выполнить
  python main.py
или
  python -m main
"""
from pathlib import Path
import sys

# Корень проекта в path для импортов src.*
_root = Path(__file__).resolve().parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from src.orchestrator import run

if __name__ == "__main__":
    run()
