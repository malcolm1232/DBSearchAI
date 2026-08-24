# Mixed-format sample corpus (Phase B verify-data)

Drop a handful of real mixed files here (a .pptx deck, a .csv, a .json export, a .docx,
a .pdf) and point the folder connector at this directory:

    FOLDER_SOURCE_DIR=$(pwd)/sample_corpus/mixed docker compose up -d

Then open the Admin console → Documents → "Verify extraction" to eyeball how each file
was parsed and chunked (slide/row/section/page locators). This is the human "verify data"
step; the automated proof lives in `scripts/dbse2e.py` (T5 slide-locator case).
