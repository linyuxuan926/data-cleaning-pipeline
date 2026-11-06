"""Generate reproducible three-format disk-engine fixtures (run with engine dependencies)."""
import csv
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]

def main():
    source=ROOT/'docs/v3/examples/input.csv'
    target=ROOT/'samples/v3-disk-orders.csv'
    target.write_bytes(source.read_bytes())
    with source.open(encoding='utf-8',newline='') as stream:
        reader=csv.DictReader(stream);data=list(reader);columns=reader.fieldnames
    frame=pd.DataFrame(data,columns=columns)
    frame.to_excel(ROOT/'samples/v3-disk-orders.xlsx',index=False,engine='openpyxl',sheet_name='订单')
    frame.to_parquet(ROOT/'samples/v3-disk-orders.parquet',index=False,engine='pyarrow')
    print('Generated identical logical string records in CSV/XLSX/Parquet; original order IDs preserved.')

if __name__=='__main__':main()
