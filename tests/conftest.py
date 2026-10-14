import sys
from pathlib import Path

# Allow `from tools.guardrails import ...` and `import agent` without installing as a package
sys.path.insert(0, str(Path(__file__).parent.parent))
