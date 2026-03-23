#!/bin/bash
# Voice to Text Mac — One-click setup

set -e

cd "$(dirname "$0")"

echo "========================================="
echo "  Voice to Text Mac — Kurulum"
echo "========================================="
echo ""

# Python kontrol
PYTHON="python3.12"
if ! command -v "$PYTHON" &> /dev/null; then
    PYTHON="python3"
fi

echo "Python: $($PYTHON --version)"
echo ""

# Virtual environment
if [ ! -d ".venv" ]; then
    echo "→ Virtual environment olusturuluyor..."
    $PYTHON -m venv .venv
fi

echo "→ Virtual environment aktif ediliyor..."
source .venv/bin/activate

echo "→ Bagimliliklar kuruluyor..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo ""
echo "========================================="
echo "  Kurulum tamamlandi!"
echo "========================================="
echo ""
echo "Baslatmak icin:  ./start.sh"
echo "Veya:            source .venv/bin/activate && python main.py"
echo ""
echo "NOT: macOS Accessibility izni gerekli!"
echo "  System Settings → Privacy & Security → Accessibility"
echo "  → Terminal (veya kullandiginiz terminal app) ekleyin"
echo ""
