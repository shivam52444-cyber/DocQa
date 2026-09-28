@echo off
aws cloudformation deploy ^
  --template-file infra/docqa-stack.yaml ^
  --stack-name docqa ^
  --region ap-south-2 ^
  --capabilities CAPABILITY_NAMED_IAM ^
  --parameter-overrides ^
    BucketName= xxxxx^
    DbMasterPassword=XXX
    LlamaBaseUrl=XXXX^
    LlamaModel=llama-3.3-70b-versatile ^
    LlamaApiKey=gsk_xxxxx ^
    OpenAiApiKey=sk-xxxx
    BackendImage= ^
    FrontendImage=