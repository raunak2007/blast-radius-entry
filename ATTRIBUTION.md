# Attribution

Two ideas on this branch come from other MIT-licensed Blast Radius entries; the code here
is our own implementation of them, on our own data files.

- Counting dict-key lookups per dict (the class whose dict it was) rather than per
  attribute name: Not-Ethan, https://github.com/Not-Ethan/blast-radius-selector
  (`selector/keyrec.py`, `tools/trace_keys.py`), MIT License.
- Splitting test_execute_tpcds's weak-mutation records into one context per TPC-DS query:
  shubham-jadhav-FiatLux, https://github.com/shubham-jadhav-FiatLux/blast-radius-selector
  (README, "The profile splits it into one context per query"), MIT License.
