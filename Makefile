PYTHON ?= .venv/bin/python
PYTHONPATH := src
ROUNDS ?= 5

.PHONY: test security preflight eval-regression eval-ui eval-quality eval-realtime eval-audio

test:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m pytest -q

security:
	AHEM_PYTHON=$(PYTHON) scripts/security-check.sh

preflight:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m meeting_host.preflight --mode local

eval-regression:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m pytest -q tests/harness tests/test_phase.py tests/test_score_run.py tests/test_packet_resilience.py

eval-ui:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m pytest -q tests/test_partial.py tests/test_spectator_chair_broken.py tests/test_spectator_phase.py tests/test_spectator_three_state.py tests/test_spectator_security_browser.py

eval-quality:
	@test -n "$(EVENTS)" -a -n "$(LABELS)" || (echo "需要 EVENTS=<events.jsonl> LABELS=<labels.json>" >&2; exit 2)
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) experiments/rescore_slow_path.py "$(EVENTS)" --labels "$(LABELS)" --rounds $(ROUNDS)

eval-realtime:
	@test "$$MEETING_HOST_RUN_REAL_DISCORD" = "1" || (echo "需要明確設定 MEETING_HOST_RUN_REAL_DISCORD=1" >&2; exit 2)
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m pytest -q tests/test_live_shutdown.py

eval-audio:
	@test -n "$(SCENARIO)" -a -n "$(OUTPUT)" || (echo "需要 SCENARIO=<scenario.json> OUTPUT=<audio.wav>" >&2; exit 2)
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) experiments/generate_synthetic_audio.py "$(SCENARIO)" --output "$(OUTPUT)"
