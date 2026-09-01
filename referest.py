import requests                                                                                                      
import json                                                                                                          
                                                                                                                       
r = requests.post(                                                                                                   
      "https://accounts.zoho.in/oauth/v2/token",                                                                       
      data={                                                                                                           
          "code": "1000.2321d8cb5247802cb0812f361a4fcc34.3324985609279c7d3e4d959db0be57c6",                                                                                               
          "client_id": "1000.0AARAVLVWX0J6QFI7JCCBUFDUVIUSL",                                                                                          
          "client_secret": "fc98183a66397a2ade027188705b2c8474cb7db256",                                                                                      
          "redirect_uri": "https://www.zoho.in",                                                                       
          "grant_type": "authorization_code",                                                                          
      },                                                                                                               
  )                                                                                                                    
                                                                                                                       
print(json.dumps(r.json(), indent=2))                                                                                
                                         