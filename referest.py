import requests                                                                                                      
import json                                                                                                          
                                                                                                                       
r = requests.post(                                                                                                   
      "https://accounts.zoho.in/oauth/v2/token",                                                                       
      data={                                                                                                           
          "code": "1000.9f406d5ba2d09db896df5abf8738cf12.12eccae9be7881111d6c493cf21ca6f5",                                                                                               
          "client_id": "1000.8XX4KXAHH42WTRLNMIKKAELEERJB4U",                                                                                          
          "client_secret": "30a29a700a827503c02680bb3de1a9d664ce8cdc8c",                                                                                      
          "redirect_uri": "https://www.zoho.in",                                                                       
          "grant_type": "authorization_code",                                                                          
      },                                                                                                               
  )                                                                                                                    
                                                                                                                       
print(json.dumps(r.json(), indent=2))                                                                                
                                         