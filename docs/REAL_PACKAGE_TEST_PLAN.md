# Real BuildingConnected package test plan

This is a local review of a bid package you already downloaded. It does not log into BuildingConnected, and it does not call production.

Use it when you want the estimate report on a real masonry or insulation set: the drawings and spec sections that were found, scope by trade, quantities that the text actually states, the sheet or spec line behind each quantity, material and waste, labor, overhead, and profit. Anything the package does not state stays blank. The package total stays blocked until those blanks are filled from a source you can name.

## Do not

- Do not point `INSOLIX_DATA_DIR` at the server `/data` volume.
- Do not run this against the hosted app.
- Do not set a BuildingConnected token, and do not open the in-app document download. The ingest API is not part of this test.
- Do not merge the draft pull request or deploy it to try this.

## What you need

- The FieldOps repo on this branch, on your own computer.
- Python 3.12 with the packages in `requirements.txt`, plus `pytest` if you also want the synthetic check.
- A BuildingConnected **Download All** zip saved from your browser while you are logged into BuildingConnected. Save the zip as a normal file. The app only reads that file from the upload form.

## Run locally

From the repo directory:

```bash
export INSOLIX_DATA_DIR="$HOME/insolix-report-trial"
export INSOLIX_AGENTS_ENABLED=0
mkdir -p "$INSOLIX_DATA_DIR"
python3 -m uvicorn main:app --host 127.0.0.1 --port 8000
```

`INSOLIX_AGENTS_ENABLED=0` keeps the agent loop from starting. Open `http://127.0.0.1:8000` and sign in with the local admin account on that trial database.

1. Create an estimator project for the bid.
2. On the project page, choose **Upload Entire Bid Package** and select the Download All zip. Leave the file name as downloaded if it contains "BuildingConnected" or "Download All". The intake record then shows `BuildingConnected Download All zip <file name>`.
3. Open **Estimate Report** on that project. Use **Download CSV** or **Download PDF** for the same report.

The trial database is only the folder in `INSOLIX_DATA_DIR`. Deleting that folder removes the trial. It does not change production.

## What the report is allowed to show

- Sheet numbers, sheet titles, and spec section numbers read from the page text. A file named like a drawing is not treated as a sheet number.
- A quantity only when the scope words and the quantity are in the same sentence, line, or table row, and the takeoff marks that row `Explicit plan/spec text`.
- A waste percent only when that same excerpt states it. Material quantity with waste is the measured quantity times `(1 + that percent)`.
- Unit costs only from a pricebook item you assign on the project. A suggestion is labeled and is not priced. A missing cost, or a material or labor cost of $0, leaves that money blank and blocks the total.
- Catalog sell price and pricebook "other" cost are shown when they are on file and are not added into the total.
- Labor hours only after you save a crew, a production rate above zero, a unit such as `SF/hour`, and a written source. Hours use the measured net quantity divided by that rate. Waste is not added to hours.
- Overhead and profit only after you save each percent and a written source on the report. A blank percent is stored as blank, not as zero. Typing `0` with a source saves zero because you entered it.
- The package total only when every scope line has a stated quantity, a stated waste factor, pricebook material and labor costs above zero, a sourced labor rate, and both overhead and profit are saved. Until then the total is **blocked** and the HTML, CSV, and PDF leave it empty.

Elevation dimensions such as `80'-0" long by 12'-0" high` are quoted and are not multiplied into square feet.

## Decisions you need to make before a total can appear

These are not filled in by the software:

| Decision | What to enter | Where |
| --- | --- | --- |
| Pricebook item | Which INSOLIX item matches the spec, after you confirm the unit costs are current | Project takeoff review, then the report shows the code and name as the price source |
| Waste factor | Use the percent written in the spec excerpt. If the spec is silent, the report stays blank until you add a measured takeoff whose evidence states the factor you are willing to stand behind | Spec excerpt, or a reviewed takeoff you enter |
| Openings and corners | Deductions are not taken from elevation dimensions | Manual takeoff on the project, with the sheet you measured |
| Crew | Who does the work, for example `4 masons` | Estimate report, labor form on that scope |
| Production rate and unit | The rate and the unit, for example `40` and `SF/hour` | Same labor form |
| Source of the labor rate | The log, quote, or note the rate came from | Same labor form. The rate is rejected if this is blank |
| Overhead percent and source | The percent you want on direct cost, and where that percent is written | Pricing form on the report. Saved rates apply to every project report on this database until you change them |
| Profit percent and source | The percent on direct cost plus overhead, and where it is written | Same pricing form |
| Zero overhead or profit | Only if you mean zero. Type `0` and a source. Leaving the box empty does not mean zero | Same pricing form |
| OCR pages | Pages with almost no text are flagged. Do not price from them until someone reads the sheet | Report drawings table |

Labor dollars on a line come from the pricebook labor cost times the measured quantity. Hours are a separate check from the production rate. The report does not invent an hourly wage to turn hours into dollars.

## Synthetic check

This does not use a real zip. From the repo:

```bash
python3 -m pytest tests/test_estimate_report.py -v --tb=short
```

That test builds a three-PDF masonry package: a floor plan titled A-101, elevations A-201 with wall dimensions, and specification section 04 20 00 for CMU and brick veneer. It checks the HTML report, the CSV, and the PDF. The stated CMU quantity is 1,240 SF with 5% waste from the spec sentence. The elevation dimensions stay unmeasured. A $0 material cost blocks the total. A labor rate with no source is rejected.

## After you review a real package

Keep the trial folder if you want the report. Do not copy that database over the production `/data` database. Pricing rates saved in the trial are local to that file.
