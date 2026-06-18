


using HTTP, JSON3, Base64

# ── Configuration ─────────────────────────────────────────────────────────────
const ACCESS_KEY   = get(ENV, "ONSHAPE_ACCESS_KEY",  "on_f6UAmXSiWh8nB69SgzzfU")
const SECRET_KEY   = get(ENV, "ONSHAPE_SECRET_KEY",  "GTO5ogi4xbomY5VtP3KCJHPwhEZUwu7AnhnKgq18ppmwE804")
const BASE_URL   = "https://cad.onshape.com"


function File_Name(did::String)

        path = "/api/partstudios/d/$did"
        url  = BASE_URL * path

        #Create authorization token for accessing the API
    token   = base64encode("$(ACCESS_KEY):$(SECRET_KEY)")
    headers = [
        "Authorization" => "Basic $token",
        "Accept"        => "application/json;charset=UTF-8;qs=0.09",
        "Content-Type"  => "application/json",
    ]

    # Optional query parameters
params = Dict()



# Make the API request
response = HTTP.get(api_url; query=params, headers=headers)

# Parse the JSON response
data = JSON3.read(String(response.body))

# Print the `name` property as formatted JSON
println(data["name"])
return(data["name"])

end