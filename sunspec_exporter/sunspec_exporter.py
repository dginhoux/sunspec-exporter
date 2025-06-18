#!/usr/bin/env python3

import sys
import time
import re
import ast
import yaml
import numbers
import collections
import logging
from pathlib import Path
from prometheus_client import start_http_server, Summary, Counter
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily, REGISTRY
import sunspec.core.client as client
import sunspec.core.suns as suns
import sunspec.core.pics as pics
from xml.dom import minidom
from xml.etree import ElementTree as ET

def setup_logger(log_level_str):
    from logging.handlers import RotatingFileHandler
    logger = logging.getLogger("sunspec_exporter")
    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        '[%(asctime)s] [%(levelname)s] %(name)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    console_handler = logging.StreamHandler(sys.stdout)
    file_handler = RotatingFileHandler("sunspec_exporter.log", maxBytes=1_000_000, backupCount=3)

    try:
        level = getattr(logging, log_level_str.upper(), logging.INFO)
    except AttributeError:
        level = logging.INFO

    console_handler.setLevel(level)
    file_handler.setLevel(logging.DEBUG)

    console_handler.setFormatter(formatter)
    file_handler.setFormatter(formatter)

    if not logger.handlers:
        logger.addHandler(console_handler)
        logger.addHandler(file_handler)

    return logger

Filter = collections.namedtuple('Filter', ['regex', 'fn'])

class FnMapping:
    @staticmethod
    def filter_fn(fn, *args):
        def filter(v):
            return fn(*args, float(v))
        return filter

    @staticmethod
    def gt(replacement, upper_bound, val):
        return replacement if val > float(upper_bound) else val

    @staticmethod
    def lt(replacement, lower_bound, val):
        return replacement if val < float(lower_bound) else val

    @staticmethod
    def gte(replacement, upper_bound, val):
        return replacement if val >= float(upper_bound) else val

    @staticmethod
    def lte(replacement, lower_bound, val):
        return replacement if val <= float(lower_bound) else val

    @staticmethod
    def equals(replacement, equals_val, val):
        return replacement if val == float(equals_val) else val

REQUEST_TIME = Summary('sunspec_fn_collect_data', 'Time spent collecting the data')

@REQUEST_TIME.time()
def collect_data(sunspec_client, model_ids, filters):
    if not sunspec_client:
        logger.warning("No SunSpec client defined, skipping collection.")
        return {}

    results = {}
    logger.info("Collecting data...")
    try:
        sunspec_client.read()
    except Exception as e:
        logger.error(f"Error reading SunSpec data: {e}")
        modbus_error.inc()
        return {}

    for model in sunspec_client.device.models_list:
        label = model_name(model)
        logger.info(f"Model: {label}")
        for block in model.blocks:
            index = f"{block.index:02d}_" if block.index > 0 else ""
            for point in block.points_list:
                if point.value is not None:
                    pt = point.point_type
                    point_label = re.sub(r'[^a-zA-Z0-9_]', '_', pt.label) if pt.label else pt.id
                    metric_label = f"{index}{point_label}_{pt.id}"
                    units = pt.units or ""
                    unit_label = f"_{units}" if units else ""
                    metric_type = "Gauge" if units else "Counter"

                    value = f"{point.value}".rstrip('\0')
                    if pt.type in (suns.SUNS_TYPE_BITFIELD16, suns.SUNS_TYPE_BITFIELD32):
                        value = f"{point.value:x}"

                    for f in filters:
                        if f.regex.match(metric_label):
                            original = value
                            value = f.fn(value)
                            logger.debug(f"Filter applied: {metric_label} {original} -> {value}")

                    final_label = f"{metric_label}{unit_label}"
                    results[final_label] = {"value": value, "metric_type": metric_type}

    if log_metrics:
        for k, v in results.items():
            logger.info(f"Collected metric: {k} -> {v['value']}")
    return results

class SunspecCollector:
    def __init__(self, sunspec_client, model_ids, ip, port, target, filters, timeout):
        self.sunspec_client = sunspec_client
        self.model_ids = model_ids
        self.ip = ip
        self.port = port
        self.target = target
        self.filters = filters
        self.timeout = timeout

    def collect(self):
        try:
            sunspec_client = client.SunSpecClientDevice(
                client.TCP,
                self.target,
                ipaddr=self.ip,
                ipport=self.port,
                timeout=self.timeout
            )
            sunspec_client.read()

            # Filter models
            sunspec_client.device.models_list = [
                model for model in sunspec_client.device.models_list
                if str(model.id) in map(str, self.model_ids)
            ]

            results = collect_data(sunspec_client, self.model_ids, self.filters)
        except Exception as e:
            logger.error(f"Failed to collect SunSpec data: {e}")
            modbus_error.inc()
            return

        for label, meta in results.items():
            val = meta["value"]
            if is_numeric(val):
                metric_class = GaugeMetricFamily if meta["metric_type"] == "Gauge" else CounterMetricFamily
                metric = metric_class(f"sunspec_{label}", '', labels=["ip", "port", "target"])
                metric.add_metric([self.ip, str(self.port), str(self.target)], float(val))
                yield metric
            else:
                logger.warning(f"Skipping non-numeric metric: {label} -> {val}")

def is_numeric(obj):
    try:
        float(obj)
        return True
    except (ValueError, TypeError):
        return False

def model_name(model):
    return f"{model.model_type.label} ({model.id})" if model.model_type.label else f"({model.id})"

def load_config(path):
    try:
        with open(path, 'r') as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.error(f"Failed to read config file: {e}")
        sys.exit(1)

def validate_config(cfg):
    required = ['mode', 'sunspec_ip', 'sunspec_port', 'sunspec_address']
    for key in required:
        if key not in cfg:
            logger.error(f"Missing required config key: {key}")
            sys.exit(1)
    if cfg['mode'] == 'start' and 'sunspec_model_ids' not in cfg:
        logger.error("Missing 'sunspec_model_ids' in 'start' mode")
        sys.exit(1)
    if cfg['mode'] == 'start' and 'port' not in cfg:
        logger.error("Missing 'port' in 'start' mode")
        sys.exit(1)

def build_filters(filter_cfg):
    fn_map = {
        "gt": FnMapping.gt,
        "lt": FnMapping.lt,
        "gte": FnMapping.gte,
        "lte": FnMapping.lte,
        "equals": FnMapping.equals
    }
    filters = []
    if filter_cfg:
        for f in filter_cfg:
            try:
                metric_regex, func_n_args, replacement = f.split(" ")
                func_name, *args = func_n_args.split(":")
                filters.append(Filter(
                    regex=re.compile(metric_regex),
                    fn=FnMapping.filter_fn(fn_map[func_name], replacement, *args)
                ))
            except Exception as e:
                logger.error(f"Invalid filter format: {f} -> {e}")
                sys.exit(1)
    return filters

def sunspec_query(ip, port, address):
    try:
        sd = client.SunSpecClientDevice(client.TCP, address, ipaddr=ip, ipport=port, timeout=10.0)
    except client.SunSpecClientError as e:
        logger.error(f"Connection error: {e}")
        sys.exit(1)

    sd.read()
    root = ET.Element(pics.PICS_ROOT)
    sd.device.to_pics(parent=root, single_repeating=True)
    print(minidom.parseString(ET.tostring(root)).toprettyxml(indent="  "))

if __name__ == '__main__':
    if len(sys.argv) != 2 or not sys.argv[1].startswith("--config=") or not sys.argv[1].split("=", 1)[1].strip():
        print("Usage: sunspec_exporter.py --config=config.yml")
        sys.exit(1)

    config_path = sys.argv[1].split("=", 1)[1]
    config = load_config(config_path)
    logger = setup_logger(config.get('log_level', 'INFO'))
    log_metrics = config.get('log_metrics', False)

    # Define global error counter
    modbus_error = Counter('modbus_error', 'Number of Modbus client connection errors')

    validate_config(config)

    sunspec_ip = config['sunspec_ip']
    sunspec_port = int(config['sunspec_port'])
    sunspec_address = config['sunspec_address']
    modbus_timeout = float(config.get('modbus_timeout', 5))  # default to 10s if not set

    if config['mode'] == 'query':
        sunspec_query(sunspec_ip, sunspec_port, sunspec_address)
        sys.exit(0)

    sunspec_model_ids = config['sunspec_model_ids']
    exporter_port = int(config['port'])
    filters = build_filters(config.get('filters'))

    sunspec_client = None
    try:
        sunspec_client = client.SunSpecClientDevice(
            client.TCP, sunspec_address, ipaddr=sunspec_ip, ipport=sunspec_port, timeout=10.0
        )
        sunspec_client.read()
    except Exception as e:
        logger.error(f"Modbus connection or read error: {e}")
        modbus_error.inc()

    if sunspec_client:
        for model in sunspec_client.device.models_list[:]:
            if str(model.id) not in map(str, sunspec_model_ids):
                sunspec_client.device.models_list.remove(model)

    REGISTRY.register(SunspecCollector(
        sunspec_client,
        sunspec_model_ids,
        sunspec_ip,
        sunspec_port,
        sunspec_address,
        filters,
        modbus_timeout
    ))

    start_http_server(exporter_port)
    logger.info(f"Exporter started on port {exporter_port}")
    while True:
        time.sleep(1000)
