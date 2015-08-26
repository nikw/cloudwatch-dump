#!/usr/bin/env python
# -*- coding: utf-8 -*-

from datetime import timedelta
from dateutil.tz import tzlocal
from boto import ec2
from boto.ec2 import cloudwatch
from util import RichDateTime

VERSION = 'cloudwatch-dump %s' % __import__('cloudwatch_dump').__version__


def get_time_range(time_str, interval):
    """
    Returns pair of RichDateTime.
    """
    d = timedelta(minutes=interval)
    if time_str:
        # parse string as localtime
        t = RichDateTime.strptime(time_str, '%Y%m%d%H%M')
    else:
        # get current time in localtime
        t = RichDateTime.from_datetime((RichDateTime.now() % d) - d)
    return t, RichDateTime.from_datetime(t + d)


def get_metrics(region):
    """
    Get all metrics in specified region.
    """
    client = cloudwatch.connect_to_region(region)
    if not client:
        raise Exception('Failed to connect to region: %s' % region)
    buf = []
    next_token = None
    while True:
        if next_token:
            result = client.list_metrics(next_token=next_token)
        else:
            result = client.list_metrics()
        buf += list(result)
        if not result.next_token:
            break
        next_token = result.next_token
    return buf


def metric_to_tag(metric, statistics, ec2_names):
    """
    Create tag string from metric and statistics.
    """
    buf = []
    buf.append(metric.connection.region.name)
    buf.append(statistics)
    buf += metric.namespace.split('/')
    for x in sorted(metric.dimensions.items()):
        # replace slash for whisper safety
        buf += ('root' if s == '/' else ec2_names.get(s, s).replace('/', '_') for s in x[1])
    buf.append(metric.name)
    return '.'.join(buf)


def get_metric_statistics(metric, start_time, end_time, statistics, unit, period):
    """
    Execute CloudWatch API to fetch statistics data.
    """
    # query with utc
    datapoints = metric.query(start_time.to_utc(), end_time.to_utc(), statistics, unit, period)

    def f(datapoint):
        # read timestamp as local time
        t = RichDateTime.from_datetime(datapoint['Timestamp'], tzlocal())
        return metric, statistics, datapoint[statistics], t

    return map(f, datapoints)


def get_data(metrics, statistics_list, start, end, period_in_min):
    """
    Get summerized CloudWatch metric status,
    then generate tuples of metric, statistics, value, and timestamp in UTC.
    """
    p = timedelta(minutes=period_in_min)

    params = ((m, start, end, s, None, p.seconds) for m in metrics for s in statistics_list)
    return (data for param in params for data in get_metric_statistics(*param))


def get_ec2_names(region):
    """
    Get dictionary of the instance id and the name from all the EC2 instances.
    """
    client = ec2.connect_to_region(region)
    instances = [x for r in client.get_all_instances() for x in r.instances]
    return dict((x.id, x.tags.get('Name')) for x in instances if x.tags.get('Name'))


def print_data(data, ec2_names):
    """
    Output value of one datapoint.
    Timestamp is converted to localtime.
    """
    m, s, v, t = data
    print('%s %.10f %d' % (metric_to_tag(m, s, ec2_names), v, t.to_local().epoch()))

def write_data_to_influxdb(client, region, data, ec2_names, debug):
    """
    Write value of one datapoint to Influxdb.
    """
    m, s, v, t = data   #metric, statistic type, value, timestamp

    if debug == True:
        # This also happens to be a format you can feed directly into opentsdb
        print "%s %d %d" % (m.name, t.to_local().epoch(), v),
        print " statistic=%s" % (s),
        print " namespace=%s" % (m.namespace),
        for d in m.dimensions.keys():
          if type(m.dimensions[d] == list):
            print " [%s=%s]" % (d, ".".join(m.dimensions[d])),
          else:
            print " %s=%s" % (d, m.dimensions[d]),
        print
    for d in m.dimensions.keys():
        json_body = [
            {
                "measurement": m.name,
                "tags": {
                    "statistic": s,
                    "namespace": m.namespace,
                    "region": region,
                    d: ".".join(m.dimensions[d])
                },
                "time": t.epoch(),
                "fields": {
                    "value": v
                }
            }
        ]
        client.write_points(json_body, time_precision='s')

def parse_args():
    """
    Parse command line arguments
    """
    from optparse import OptionParser

    parser = OptionParser(version=VERSION)
    parser.add_option(
        '--region', dest='region', default='us-east-1', type='string',
        help='the name of the region to connect to'
    )
    parser.add_option(
        '--time', dest='time', default=None, type='string',
        help='start time of the query in format "YYYYMMDDhhmm" localtime'
    )
    parser.add_option(
        '--interval', dest='interval', default=60, type='int',
        help='minutes of time range in the query'
    )
    parser.add_option(
        '--period', dest='period', default=5, type='int',
        help='minutes to aggregate in the query'
    )
    parser.add_option(
        '--resolve', action='store_true', dest='resolve', default=False,
        help='replaces name tag value for EC2 instances instead of the instance id (default: False)'
    )
    parser.add_option(
        '--check', action='store_true', dest='check', default=False,
        help='prints only the metrics and its statistics methods (default: False)'
    )
    output_choices=['graphite-text', 'influxdb']
    parser.add_option(
        '--output', dest='output', default='graphite-text', type='choice',
        choices=output_choices,
        help='how to present results (choices: %s)' % (output_choices)
    )
    parser.add_option(
        '--influxdb-database', dest='influxdb_database', default='cloudwatch', type='string',
        help='if output is influxdb, the name of the influxdb database'
    )
    return parser.parse_args()


def main():
    """
    Main function
    """
    statistics_list = ['Average', 'Sum']

    # get command line arguments
    options, args = parse_args()

    # calculate time range
    start, end = get_time_range(options.time, options.interval)

    # get metrics list
    metrics = get_metrics(options.region)
    query_params = ((m, s) for m in metrics for s in statistics_list)

    # get ec2 names resolver
    ec2_names = get_ec2_names(options.region) if options.resolve else {}

    if options.check:
        # print all query params when check mode
        print('start : %s' % start)
        print('end   : %s' % end)
        print('period: %s min' % options.period)
        print('ec2 names: %s' % ec2_names)
        for q in query_params:
            print('will collect metric: %s' % (metric_to_tag(q[0], q[1], ec2_names)))
    else:
        if options.output == 'graphite-text':
            # fetch and print metrics statistics
            for data in get_data(metrics, statistics_list, start, end, options.period):
                print_data(data, ec2_names)
        elif options.output == 'influxdb':
            import influxdb
            client = influxdb.InfluxDBClient('localhost', 8086, 'root', 'root', options.influxdb_database)
            try:
                client.create_database(options.influxdb_database)
            except influxdb.exceptions.InfluxDBClientError:
                pass
            for data in get_data(metrics, statistics_list, start, end, options.period):
                write_data_to_influxdb(client, options.region, data, ec2_names, False)
        else:
            raise Exception("Invalid output option", options.output)
    return 0


if __name__ == '__main__':
    main()
