all: data run eval test
data:
	python3 generate_data.py --n 500 --seed 42
run:
	python3 run_agent.py
eval:
	python3 evaluate.py
test:
	python3 -m unittest discover -s tests -v
