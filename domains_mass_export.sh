for domain in $(cat domains.txt); do
uv run python -u cli.py --db gay_all_database export \
  --preset asian \
  --random 25000 \
  --longtails longtails_gay.txt --longtail-insert \
  -o "$domain".txt
done
