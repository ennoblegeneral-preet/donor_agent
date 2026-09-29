import requests                                                                                                      
import json                                                                                                          
                                                                                                                       
r = requests.post(                                                                                                   
      "https://accounts.zoho.in/oauth/v2/token",                                                                       
      data={                                                                                                           
          "code": "1000.90b3db811f0318c99b9139b4f3ff1b3b.e0a5e0f2c6842ea06cbc2beac8d44e31",                                                                                               
          "client_id": "1000.0AARAVLVWX0J6QFI7JCCBUFDUVIUSL",                                                                                          
          "client_secret": "fc98183a66397a2ade027188705b2c8474cb7db256",                                                                                      
          "redirect_uri": "https://www.zoho.in",                                                                       
          "grant_type": "authorization_code",                                                                          
      },                                                                                                               
  )                                                                                                                    
                                                                                                                       
print(json.dumps(r.json(), indent=2))                                                                                
                                         