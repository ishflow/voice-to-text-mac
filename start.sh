#!/bin/bash
# Voice to Text Mac — Start

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "Hata: .venv bulunamadi. Once setup.sh calistirin."
    exit 1
fi

source .venv/bin/activate
python main.py
