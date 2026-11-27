#!/usr/bin/env sh
# Прогон одной командой: ./run.sh docs/requirements.pdf
python -m validator "${1:-docs/requirements.pdf}" -o reports/report.md --gold gold/defects.md
