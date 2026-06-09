

"""
onshape_cad_download.jl

Download CAD files from Onshape using Basic Authentication.
Get your API keys from https://dev-portal.onshape.com

Usage: julia onshape_cad_download.jl
  or:  include("onshape_cad_download.jl"); demo()
"""

using HTTP
using JSON3
using Base64

# ── Configuration ─────────────────────────────────────────────────────────────
const ACCESS_KEY   = get(ENV, "ONSHAPE_ACCESS_KEY",  "on_f6UAmXSiWh8nB69SgzzfU")
const SECRET_KEY   = get(ENV, "ONSHAPE_SECRET_KEY",  "GTO5ogi4xbomY5VtP3KCJHPwhEZUwu7AnhnKgq18ppmwE804")
const BASE_URL   = "https://cad.onshape.com"

# ── Auth header (Basic) ───────────────────────────────────────────────────────
function auth_headers(content_type="application/json")
    token = base64encode("$(ACCESS_KEY):$(SECRET_KEY)")
    return [
        "Authorization" => "Basic $token",
        "Accept"        => "application/json;charset=UTF-8;qs=0.09",
        "Content-Type"  => content_type,
    ]
end

# ── HTTP helpers ──────────────────────────────────────────────────────────────
function api_get(path; query="")
    url  = BASE_URL * path * (isempty(query) ? "" : "?" * query)
    println("  [GET] $url")
    resp = HTTP.get(url, auth_headers(); status_exception=false)
    println("  [HTTP $(resp.status)]")
    if resp.status >= 400
        println("  [ERROR] $(String(resp.body))")
        error("HTTP $(resp.status) on GET $path")
    end
    return JSON3.read(resp.body)
end

function api_post(path, body::Dict)
    url  = BASE_URL * path
    println("  [POST] $url")
    resp = HTTP.post(url, auth_headers(), JSON3.write(body); status_exception=false)
    println("  [HTTP $(resp.status)]")
    if resp.status >= 400
        println("  [ERROR] $(String(resp.body))")
        error("HTTP $(resp.status) on POST $path")
    end
    return JSON3.read(resp.body)
end

function api_download(path, output_path)
    url  = BASE_URL * path
    println("  [DOWNLOAD] $url")
    resp = HTTP.get(url; headers=auth_headers(), redirect=true, status_exception=false)
    println("  [HTTP $(resp.status)]  $(length(resp.body)) bytes")
    if resp.status >= 400
        println("  [ERROR] $(String(resp.body))")
        error("HTTP $(resp.status) downloading $path")
    end
    if isempty(resp.body)
        error("Empty response body — nothing to write")
    end
    write(output_path, resp.body)
    println("  ✓ Saved → $output_path")
    return output_path
end

# ── Document helpers ──────────────────────────────────────────────────────────
function list_documents(; limit=10)
    return api_get("/api/documents"; query="limit=$limit")
end

function list_elements(did, wid)
    return api_get("/api/documents/d/$did/w/$wid/elements")
end

function list_parts(did, wid, eid)
    return api_get("/api/parts/d/$did/w/$wid/e/$eid")
end

# ── Synchronous STL export ────────────────────────────────────────────────────
function download_stl(did, wid, eid;
                      output_path="output.stl",
                      units="millimeter",
                      mode="binary")
    println("\nDownloading STL…")
    path = "/api/partstudios/d/$did/w/$wid/e/$eid/stl"
    url  = BASE_URL * path * "?mode=$mode&units=$units&grouping=true"
    resp = HTTP.get(url, auth_headers();
                    redirect=true,
                    status_exception=false)
    println("  [HTTP $(resp.status)]  $(length(resp.body)) bytes")
    if resp.status >= 400
        println("  [ERROR] $(String(resp.body))")
        error("STL download failed")
    end
    write(output_path, resp.body)
    println("  ✓ Saved → $output_path")
    return output_path
end

# ── Async translation (STEP, IGES, Parasolid, OBJ …) ─────────────────────────
function request_translation(did, wid, eid, element_type;
                              format="STEP",
                              part_ids=String[])
    body = Dict{String,Any}(
        "formatName"       => format,
        "storeInDocument"  => false,
        "allowFaultyParts" => true,
    )
    if !isempty(part_ids)
        body["partIds"] = join(part_ids, ",")
    end
    endpoint = element_type == :assembly ?
        "/api/assemblies/d/$did/w/$wid/e/$eid/translations" :
        "/api/partstudios/d/$did/w/$wid/e/$eid/translations"
    resp = api_post(endpoint, body)
    tid  = string(resp["id"])
    println("  Translation ID: $tid")
    return tid
end

function poll_translation(tid; timeout=300, interval=3)
    deadline = time() + timeout
    while time() < deadline
        resp  = api_get("/api/translations/$tid")
        state = string(resp["requestState"])
        print("\r  State: $state          ")
        if state == "DONE"
            println()
            ext_ids = get(resp, "resultExternalDataIds", nothing)
            println("  resultExternalDataIds: $ext_ids")
            return resp
        elseif state == "FAILED"
            println()
            reason = get(resp, "failureReason", "unknown")
            error("Translation FAILED: $reason")
        end
        sleep(interval)
    end
    error("Translation timed out after $(timeout)s")
end

function download_translation_result(did, ext_id, output_path)
    return api_download("/api/documents/d/$did/externaldata/$ext_id", output_path)
end

# ── High-level export functions ───────────────────────────────────────────────
function export_partstudio(did, wid, eid;
                            format="STEP",
                            output_path="output.step",
                            part_ids=String[])
    println("\nExporting Part Studio as $format…")
    tid  = request_translation(did, wid, eid, :partstudio; format=format, part_ids=part_ids)
    resp = poll_translation(tid)
    ext_ids = get(resp, "resultExternalDataIds", nothing)
    isnothing(ext_ids) || isempty(ext_ids) && error("No result data IDs in response")
    return download_translation_result(did, string(ext_ids[1]), output_path)
end

function export_assembly(did, wid, eid;
                          format="STEP",
                          output_path="output.step")
    println("\nExporting Assembly as $format…")
    tid  = request_translation(did, wid, eid, :assembly; format=format)
    resp = poll_translation(tid)
    ext_ids = get(resp, "resultExternalDataIds", nothing)
    isnothing(ext_ids) || isempty(ext_ids) && error("No result data IDs in response")
    return download_translation_result(did, string(ext_ids[1]), output_path)
end

# ── Demo ──────────────────────────────────────────────────────────────────────
function demo()
    # Replace with your IDs from the document URL:
    # https://cad.onshape.com/documents/{DID}/w/{WID}/e/{EID}
    DID = "6c0d2b9b726f93f6e30525f2"
    WID = "e60b006b496816beb3fba67d"
    EID = "9524668d3f639b4055de0e2b"

    println("=" ^ 60)
    println("Onshape CAD Downloader")
    println("Working dir: $(pwd())")
    println("=" ^ 60)

    println("\n[1] Auth check — listing documents…")
    docs = list_documents(limit=5)
    for d in get(docs, "items", [])
        println("  • $(d["name"])  ($(d["id"]))")
    end

    println("\n[2] Elements in document…")
    elements = list_elements(DID, WID)
    for el in elements
        println("  • $(el["name"])  type=$(el["elementType"])  eid=$(el["id"])")
    end

    println("\n[3] Export as STEP…")
    export_partstudio(DID, WID, EID;
                      format="STEP",
                      output_path=joinpath("E:/Onshape test", "output.step"))

    #println("\n[4] Export as STL…")
    #download_stl(DID, WID, EID; output_path=joinpath(pwd(), "output.stl"))

    println("\nDone.")
end

if abspath(PROGRAM_FILE) == @__FILE__
    demo()
end

