# Catalog review worksheet

`catalog-review-worksheet.csv` lists the 31 catalog values the sample GPU node (v1.1) relies on
most. **No row has been checked.** A named person must open each datasheet, then fill in
`decision` (`ok` or `correct`), `corrected_value` if needed, `datasheet_source` (document and
page) and `checked_by` (their own name; agents are refused). Leave unchecked rows blank.

    vcm catalog review --from-csv docs/review/catalog-review-worksheet.csv

The command validates every row first and applies none if any is wrong. Nothing in the bundled
catalog is `reviewed: true` until that has been done.
