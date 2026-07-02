#!/usr/bin/env bash
#curl -s -X POST localhost:8000/libraries/1/documents  -F 'files=@/home/masterkenway/Downloads/ocr_input/sales_test.csv' -F 'files=@/home/masterkenway/Downloads/ocr_input/hearing_iconix.docx'
for file in /home/masterkenway/Downloads/ocr_input/*; do
    [ -f "$file" ] || continue
    curl -s -X POST -F "files=@$file" localhost:8000/libraries/1/documents
    echo
done
