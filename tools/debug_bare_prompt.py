import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

import openpyxl
from citadel.services.excel import load_all_sheets, render_sheet_dump
from citadel.llm import _post_lm, LM_MODEL, SAMPLING

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_bare_prompt(file_path: str, target_sheet_name: str, prompt_prefix: str = ""):
    logger.info(f"Processing {file_path} -> {target_sheet_name}")
    
    try:
        with open(file_path, "rb") as f:
            data = f.read()
        
        sheets = load_all_sheets(data)
        sheet = next((s for s in sheets if s.sheet_name == target_sheet_name), None)
        
        if sheet is None:
            logger.error(f"Sheet {target_sheet_name} not found in {file_path}")
            return

        dump_text = render_sheet_dump(sheet, file_path)
        logger.info(f"Dump generated ({len(dump_text)} chars)")
        
        full_prompt = f"{prompt_prefix}\n\n{dump_text}"
        
        # Construct the correct payload for vLLM/Gemma 4
        payload = {
            "model": LM_MODEL,
            "messages": [{"role": "user", "content": full_prompt}],
            **SAMPLING,
            "max_tokens": 4096,
            "chat_template_kwargs": {"enable_thinking": False},
            "reasoning_effort": "none",
        }
        
        logger.info("Sending bare prompt to Gemma 4...")
        response = await _post_lm(payload)
        
        print("\n" + "="*50)
        print(f"RESULTS FOR {target_sheet_name}")
        print("="*50)
        print(response)
        print("="*50 + "\n")

    except Exception as e:
        logger.exception(f"Failed to process {target_sheet_name}: {e}")

async def main():
    targets = [
        ("/home/masterkenway/Downloads/ocr_input/1.xlsx", "Fuel"),
        ("/home/masterkenway/Downloads/ocr_input/1.xlsx", "Balancing"),
        ("/home/masterkenway/Downloads/ocr_input/8.xlsx", "Fugitives"),
    ]
    
    bare_prompt = "Extract the tables from this spreadsheet dump. Return the structure and rows."
    
    for file_path, sheet_name in targets:
        await test_bare_prompt(file_path, sheet_name, bare_prompt)

if __name__ == "__main__":
    asyncio.run(main())
