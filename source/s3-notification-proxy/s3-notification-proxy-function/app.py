import os, re, gzip, boto3, json, urllib.parse, urllib.request, traceback, datetime, calendar, sys, logging
import dateutil.parser as dp
from base64 import b64decode
logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
cloudwatch = boto3.client('logs')
ssm = boto3.client('ssm')
logger.info("Starting Execution")

def get_configuration_param(configTag):
  try:
    ssmPath = "/lambda/s3NotifyProxy/" + configTag
    parameter=ssm.get_parameter(Name=ssmPath)
  except Exception as e:
    print(e)
    raise(e)
  return parameter['Parameter']['Value']


def get_logstream(bucket,key):
  logGroupName = "/aws/s3/" + bucket

  try:
    cloudwatch.create_log_group(logGroupName=logGroupName)
  except cloudwatch.exceptions.ResourceAlreadyExistsException:
    pass
  try:
    cloudwatch.create_log_stream(logGroupName=logGroupName, logStreamName=key)
  except cloudwatch.exceptions.ResourceAlreadyExistsException:
    pass
  response = cloudwatch.describe_log_streams(
    logGroupName=logGroupName,
    logStreamNamePrefix=key
  )
  return response, logGroupName, key


def get_timestamp(datetime_string):
  dt_string = str(datetime_string)
  if 'unix' in datetime_format_string:
    return dt_string
  if datetime_format_string == 'unix_ms':
    return dt_string
  if 'ISO8601' in datetime_format_string:
    parsed_t = dp.parse(datetime_string)
    msTimeStamp = parsed_t.strftime('%s%f')
    msTimeStamp = msTimeStamp[:-3]
    return int(msTimeStamp)


def parse_lines(lines_read, bucket_name):
  removed_log_size = 0
  parsed_lines = []
  for line in lines_read:
    line = line.decode('utf-8', 'ignore').strip()
    if line:
      try:
        matcher = custom_regex.search(line)
        if matcher:
          log_fields = matcher.groupdict(default='-')
          for field_name in ignored_fields:
            removed_log_size += len(log_fields.pop(field_name, ''))
          timestamp = get_timestamp(log_fields[logTypeConfig['dateField']])
          formatted_line = {'timestamp': timestamp,
                            's3bucket': bucket_name}
          formatted_line.update(log_fields)
          parsed_lines.append(formatted_line)
      except Exception as e:
        traceback.print_exc()
        print(e)
  return parsed_lines, removed_log_size


def is_filters_matched(formatted_line):
  if 'filterConfig' in logTypeConfig:
    for config in logTypeConfig['filterConfig']:
      if config in formatted_line and (
          filter_config[config]['match'] ^ (formatted_line[config] in filter_config[config]['values'])):
        return False
  return True


def get_json_value(obj, key):
  if key in obj:
    return obj[key]
  elif '.' in key:
    parent_key = key[:key.index('.')]
    child_key = key[key.index('.') + 1:]
    return get_json_value(obj[parent_key], child_key)


def json_log_parser(lines_read, bucket_name):
  log_size = 0
  parsed_lines = []
  lines = lines_read.decode('utf-8').splitlines()
  records_obj = []
  i = 0
  for line in lines:
    records_obj.append(json.loads(line))
  for event_obj in records_obj:
    formatted_line = {}
    for path_obj in logTypeConfig['jsonPath']:
      value = get_json_value(event_obj, path_obj['key' if 'key' in path_obj else 'name'])
      if value:
        formatted_line[path_obj['key']] = value
        log_size += len(str(value))
    if not is_filters_matched(formatted_line):
      continue
    formatted_line['timestamp'] = int(get_timestamp(formatted_line[logTypeConfig['dateField']]))
    formatted_line['s3bucket'] = bucket_name
    parsed_lines.append(formatted_line)
  return parsed_lines, log_size


def send_logs(parsedLines,logGroupName, logStreamName, logResponse):
  logEvents = []
  sequenceToken = 0
  print("sending logs to " + logGroupName + " " + logStreamName)
  if 'uploadSequenceToken' in logResponse['logStreams'][0]:
    sequenceToken = logResponse['logStreams'][0]['uploadSequenceToken']
  lines = 0
  sizeOfPayload = 0
  for line in parsedLines:
    lines = lines + 1
    timeStamp = line['timestamp']
    event = {
      'timestamp': timeStamp,
      'message': json.dumps(line)
    }
    logEvents.append(event)
    sizeOfPayload = sizeOfPayload + sys.getsizeof(line)
    # If we hit our size limit for sending items to cloudwatch send what we have and continue.
    if (sizeOfPayload > 150000) or (lines > 8000):
      print("sending batch")
      newEvents = sorted(logEvents, key=lambda k: k['timestamp'])
      try:
        response = cloudwatch.put_log_events(logGroupName=logGroupName, logStreamName=logStreamName, logEvents=newEvents,
                                           sequenceToken=str(sequenceToken))
      except Exception as e:
        print(e)
        raise (e)
      if 'nextSequenceToken' in response:
        sequenceToken = response['nextSequenceToken']
      logEvents = []
      lines = 0
      sizeOfPayload = 0

  print("sending last batch")
  try:
    newEvents = sorted(logEvents, key=lambda k: k['timestamp'])
    response = cloudwatch.put_log_events(logGroupName=logGroupName, logStreamName=logStreamName, logEvents=newEvents, sequenceToken=str(sequenceToken))
  except Exception as e:
    print(e)
    raise(e)


def lambda_handler(event, context):
  # Get the object from the event
  account_id = boto3.client('sts').get_caller_identity().get('Account')
  logger.info(f"Got Account ID {account_id}")
  bucket = event['Records'][0]['s3']['bucket']['name']
  key = urllib.parse.unquote_plus(event['Records'][0]['s3']['object']['key'], encoding='utf-8')
  logger.info(f"Bucket: {bucket}")
  logger.info(f"Key: {key}")
  tags = s3.get_bucket_tagging(Bucket=bucket)
  configTag = None
  logger.info(f"Tags: {tags}")
  for tag in tags['TagSet']:
    # Search for tags associated to the s3 object to proxy notifications to
    if (tag['Key'] == "au:s3-notification-proxy"):
      # If we find the base tag set the configTag to the ssm parameter variable to use to parse the log file to cloudwatch
      logger.info(f"Found Tag: {tag['Key']}")
      logger.info(f"Value: {tag['Value']}")
      configTag = tag['Value']

    if ("au:s3-notification-proxy:sqsrelay" in tag['Key']):
      # If we find the sqsrelay tag then forward the message to the sqs queue identified
      logger.info(f"Found Tag: {tag['Key']}")
      logger.info(f"Value: {tag['Value']}")
      sqs = boto3.client('sqs')
      value_list = tag['Value'].split(" ")
      key_prefix = value_list[0]
      queue_url = value_list[1]
      for record in event['Records']:
        if key_prefix in record['s3']['object']['key']:
          logger.info("Forwarding record to SQS")
          sqs_params = {
            "Records": [ record ]
          }
          try:
            response = sqs.send_message(
              QueueUrl=queue_url,
              DelaySeconds=10,
              MessageBody=json.dumps(sqs_params)
            )
            logger.info(response)
          except Exception as e:
            print(e)
            raise(e)

    if ("au:s3-notification-proxy:lambdarelay" in tag['Key']):
      # If we find the lambdarelay key then forward the record to that function
      logger.info(f"Found Tag: {tag['Key']}")
      logger.info(f"Value: {tag['Value']}")
      lambdaClient = boto3.client('lambda')
      value_list = tag['Value'].split(" ")
      function_name = value_list[1]
      key_prefix = value_list[0]
      for record in event['Records']:
        if key_prefix in record['s3']['object']['key']:
          logger.info("Forwarding record to lambda")
          input_params = {
            "Records": [ record ]
          }
          try:
            response = lambdaClient.invoke(
              FunctionName=function_name,
              InvocationType="RequestResponse",
              Payload=json.dumps(input_params)
            )
            response_payload = json.load(response['Payload'])
            logger.info(response_payload)
          except Exception as e:
            print(e)
            raise(e)

  if configTag is not None:
    # If we have previously foind a config tag then unpack the file or read the file in directly
    logger.info(f"Found Configuration Tag: {configTag} on Bucket {bucket}")
    response = s3.get_object(Bucket=bucket, Key=key)
    logResponse, logGroupName, logStreamName = get_logstream(bucket=bucket,key=key)
    if response['ContentType'] == 'application/x-gzip' or key.endswith('.gz'):
      lines_read = gzip.decompress(response['Body'].read())
    else:
      lines_read = response['Body'].read()

    # Get the logging config from ssm, yes this sets a bunch of globals.
    configParam = get_configuration_param(configTag=configTag)
    global logTypeConfig
    logTypeConfig = json.loads(b64decode(configParam).decode('utf-8'))
    global custom_regex
    custom_regex = re.compile(logTypeConfig['regex']) if 'regex' in logTypeConfig else None
    global ignored_fields
    ignored_fields = logTypeConfig['ignored_fields'] if 'ignored_fields' in logTypeConfig else []
    global datetime_format_string
    datetime_format_string = logTypeConfig['dateFormat']
    if 'jsonPath' in logTypeConfig:
      parsed_lines, log_size = json_log_parser(lines_read, bucket)
    else:
      parsed_lines, removed_log_size = parse_lines(lines_read.split(b'\n'), bucket)
      log_size = response['ContentLength'] - removed_log_size
      logger.info('log_size : {}'.format(log_size))
    if parsed_lines:
      # Send the lines to cloudwatch
      logger.info("Sending lines")
      send_logs(parsedLines=parsed_lines,logGroupName=logGroupName, logStreamName=logStreamName, logResponse=logResponse)
  return {
    'statusCode': 200,
    'body': json.dumps('Logs Uploaded Successfully')
  }