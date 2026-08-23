# Build from the REPO ROOT so the solver/ directory is in the context:
#   docker build -f backend/Dockerfile -t smokies-solver .
FROM python:3.12-slim

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY solver/smokies_circuit_solver_20260509a.py \
     solver/smokies_edge_list_20260509a.csv \
     /app/solver/
# Slope histograms.  Without these, any request setting a custom hiking pace is
# refused: the alternative would be falling back to the baked cost columns,
# which silently mixes two pace models inside one solve.
COPY docs/data/segment_profiles.json /app/solver/

COPY backend/app.py .

ENV SOLVER_DIR=/app/solver
# Cloud Run injects PORT; default to 8080 for local docker runs.
CMD exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}
