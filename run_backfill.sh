#!/bin/bash
cd /sessions/admiring-nifty-dijkstra/mnt/outputs
LOG=/sessions/admiring-nifty-dijkstra/mnt/outputs/backfill.log
echo "=== Backfill started $(date) ===" > $LOG

echo "--- meta ---" >> $LOG
python3 ingest_new5amcs.py --amc meta >> $LOG 2>&1
echo "--- quant ---" >> $LOG
python3 ingest_new5amcs.py --amc quant >> $LOG 2>&1
echo "--- uti ---" >> $LOG
python3 ingest_new5amcs.py --amc uti >> $LOG 2>&1
echo "--- tata ---" >> $LOG
python3 ingest_new5amcs.py --amc tata >> $LOG 2>&1
echo "--- motilal ---" >> $LOG
python3 ingest_new5amcs.py --amc motilal >> $LOG 2>&1
echo "--- bandhan ---" >> $LOG
python3 ingest_new5amcs.py --amc bandhan >> $LOG 2>&1
echo "=== Backfill complete $(date) ===" >> $LOG
