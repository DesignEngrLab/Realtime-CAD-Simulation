
"""
step_volume.jl

Two approaches to compute the volume of a STEP file:

  1. volume_from_stl(path)  — compute volume from an STL file directly in Julia
                              (fast, no external tools needed)

  2. volume_from_step_via_onshape(did, wid, eid) — ask Onshape for the mass
                              properties directly via its API (most accurate,
                              uses the exact B-rep geometry, no tessellation error)

Usage:
    include("step_volume.jl")

    # If you already have an STL file:
    v = volume_from_stl("output.stl")
    println("Volume: v mm³")

    # Or get it straight from Onshape (most accurate):
    
"""

using Base64
using HTTP, JSON3


# ─────────────────────────────────────────────────────────────────────────────
# APPROACH 2 — Mass properties directly from Onshape API (most accurate)
# ─────────────────────────────────────────────────────────────────────────────

# Reuse credentials from onshape_cad_download.jl if already loaded,
# otherwise define them here

    const ACCESS_KEY   = get(ENV, "ONSHAPE_ACCESS_KEY",  "on_f6UAmXSiWh8nB69SgzzfU")
    const SECRET_KEY   = get(ENV, "ONSHAPE_SECRET_KEY",  "GTO5ogi4xbomY5VtP3KCJHPwhEZUwu7AnhnKgq18ppmwE804")
    const BASE_URL   = "https://cad.onshape.com"


"""
    volume_from_onshape(did, wid, eid; part_id=nothing) -> Dict

Fetch mass properties for a Part Studio from the Onshape API.
Returns a Dict with :volume, :mass, :surface_area, :density, :centroid.

`part_id` is optional — if omitted, returns properties for all parts combined.

Units follow the document settings (usually mm/kg).
"""
function volume_from_onshape(did::String, wid::String, eid::String;
                              part_id::Union{String,Nothing}=nothing)
    path = "/api/partstudios/d/$did/w/$wid/e/$eid/massproperties"
    url  = BASE_URL * path

    token   = base64encode("$(ACCESS_KEY):$(SECRET_KEY)")
    headers = [
        "Authorization" => "Basic $token",
        "Accept"        => "application/json;charset=UTF-8;qs=0.09",
        "Content-Type"  => "application/json",
    ]

    query = isnothing(part_id) ? "" : "?partId=$part_id"
    println("Fetching mass properties from Onshape…")
    resp = HTTP.get(url * query, headers; status_exception=false)
    println("  [HTTP $(resp.status)]")

    if resp.status >= 400
        println("  [ERROR] $(String(resp.body))")
        error("HTTP $(resp.status) fetching mass properties")
    end

    data   = JSON3.read(resp.body)
    bodies = data["bodies"]

    results = Dict[]
    for (part_key, props) in pairs(bodies)
        vol          = props["volume"][1]
        mass         = props["mass"][1]
        surface_area = props["periphery"][1]
        centroid     = props["centroid"]
        push!(results, Dict(
            :part_id      => string(part_key),
            :volume_mm3   => vol * 1e9,        # m³ → mm³
            :volume_cm3   => vol * 1e6,        # m³ → cm³
            :volume_m3    => vol,
            :mass_kg      => mass,
            :surface_area_mm2 => surface_area * 1e6,
            :centroid     => centroid,
        ))
    end

    println("\n  Mass Properties:")
    println("  " * "─"^40)
    for r in results
        println("  Part: $(r[:part_id])")
        println("    Volume:       $(round(r[:volume_mm3], digits=2)) mm³")
        println("    Volume:       $(round(r[:volume_cm3], digits=4)) cm³")
        println("    Mass:         $(round(r[:mass_kg]*1000, digits=4)) g")
        println("    Surface area: $(round(r[:surface_area_mm2], digits=2)) mm²")
        println("  " * "─"^40)
    end

    return results
end

# ─────────────────────────────────────────────────────────────────────────────
# Demo
# ─────────────────────────────────────────────────────────────────────────────

function demo_volume()




    # ── Method 2: direct from Onshape API (exact B-rep result) ───────────────
    println("\n[Method 2] Volume from Onshape mass properties API")
    DID = "6c0d2b9b726f93f6e30525f2"
    WID = "e60b006b496816beb3fba67d"
    EID = "045a083a25ff8005c749039f"   # must be a Part Studio, not Assembly
    volume_from_onshape(DID, WID, EID)
end

if abspath(PROGRAM_FILE) == @__FILE__
    demo_volume()
end


 