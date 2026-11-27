#!/usr/bin/env sh
# Прогон одной командой: ./run.sh [документ]   (по умолчанию docs/requirements.md)
python -m validator "${1:-docs/requirements.md}" -o reports/report.md --gold gold/defects.md
