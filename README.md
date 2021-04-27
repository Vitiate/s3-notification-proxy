# s3-notification-proxy

This AWS Lambda function allows you to proxy an s3 notification SQS and Lambdas endpoints. In addition the function also contains the ability to ship log files stored in s3 to CloudWatch.

# Tags

The configuration of thise function is done largely in tags. Tags are attached to the s3 bucket and the value of those tags is used to identify what to forward and where to forward it to.

```
au:s3-notification-proxy    SSM Parameter /lambda/NotifyProxy/<Tag Value>
```
The above tag is used to ship logs from s3 to CloudWatch. The SSM parameter referenced byt the tag contains a Base64 
encoded json string which defines the configuration of the parser. Two example parser configurations are included, WAF 
and ELB parsers.

```
au:s3-notification-proxy:sqsrelay     <Prefix> <SQS Queue URL>
```
This tag is used to forward a s3 notification to a SQS Queue. The Resource Access policy of the Queue must allow the 
calling account to perform a SQS:SendMessage action on it.
The function will split the Prefix from the Queue URL and use the Prefix to perform a like string compare to the Key 
sent by the s3 Notification. This way you can match on a substring of the key. You can then send that notification to 
multiple endpoints.

```
au:s3-notification-proxy:lambdarelay <Prefix> <Lambda Function ARN>
```
The lambda relay tag does the same thing that the sqs relay tag does. But it sends the notification to a Lambda function 
the resource access policy of the called lambda function must allow this function to call it.

#IAM Permissions
There are example permissions stored with in the template.yaml file. These permissions allow this function to access 
other aws services either locally to the functions account or cross account. Be sure to allow invoke access to lambda
functions that this function may be executing.

#Multiple Endpoint Configs
If you have more than one lambda function or SQS endpoint, simply append a number to the end of the tag:
```
au:s3-notification-proxy:sqsrelay0     <Prefix> <SQS Queue URL>
au:s3-notification-proxy:sqsrelay1    <Prefix> <SQS Queue URL>
au:s3-notification-proxy:sqsrelay2    <Prefix> <SQS Queue URL>
```
All of the tags will recieve a notification, either for the same Prefix or multiple Prefixes. There is no limit on the 
number of notifications you can forward.
