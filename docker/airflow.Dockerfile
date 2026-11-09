ARG AIRFLOW_VERSION=3.2.2
ARG PYTHON_VERSION=3.12
FROM apache/airflow:${AIRFLOW_VERSION}-python${PYTHON_VERSION}

ARG AIRFLOW_VERSION
ARG PYTHON_VERSION

COPY requirements-airflow.txt /opt/project/
RUN pip install --no-cache-dir "apache-airflow==${AIRFLOW_VERSION}" \
        -r /opt/project/requirements-airflow.txt

# Папка под том с моделями
RUN mkdir -p /opt/airflow/artifacts

COPY --chown=airflow:root common /opt/project/common
COPY --chown=airflow:root etl /opt/project/etl
COPY --chown=airflow:root ml /opt/project/ml
COPY --chown=airflow:root pipeline /opt/project/pipeline
COPY --chown=airflow:root sql /opt/project/sql
COPY --chown=airflow:root dags /opt/airflow/dags

ENV PYTHONPATH=/opt/project
