import requests                                                                                                      
import json                                                                                                          
                                                                                                                       
r = requests.post(                                                                                                   
      "https://accounts.zoho.in/oauth/v2/token",                                                                       
      data={                                                                                                           
          "code": "1000.dade9595abdf9e2ad7638e55c85c5cee.35833add7031fddc454746897e46e555",                                                                                               
          "client_id": "1000.PTCLMEB8TIAQBTC8PP1U20LOVU3I7I",                                                                                          
          "client_secret": "af8e55dc767cda29f50db05c6988cf6dc7b337175a",                                                                                      
          "redirect_uri": "https://www.zoho.in",                                                                       
          "grant_type": "authorization_code",                                                                          
      },                                                                                                               
  )                                                                                                                    
                                                                                                                       
print(json.dumps(r.json(), indent=2))                                                                                
                                         