

#Main program for checking for changes and downloanding step files when seen
using Dates

#Base directory locatoin on PC
const base_loc = "E:/Onshape test"

#Pull current date/time
current_datetime = Dates.now()
current_datetime = Dates.format(current_datetime, "yy_mm_dd_HH_MM")

#Set fodler path to the base location with a new folder labelled date and time of start of experiment
folder = "$(base_loc)/$(current_datetime)"

#If folder doesn't exist create it
if !isdir(folder)
    mkdir(folder)
end

#Setup variables for Onshape CAD
#DocumentID
    DID = "6c0d2b9b726f93f6e30525f2"
#WorkspaceID
    WID = "e60b006b496816beb3fba67d"
#Assembly ID for studio
    EIDs = "045a083a25ff8005c749039f" 
#Assembly ID for Assembly
    EIDa = "9524668d3f639b4055de0e2b"

#Pull assembly file
name = File_Name(DID)

#Setup Iteration counter, initial mass measurement, model iteration and timer
i = 0
MassPrevious = nothing
model_number = 0
timer = 0.0
#While loop to setup number of iterations (i <= max#), or how much time to run (timer <= max time in seconds)
while timer <= 10 
    timer_current = time()
    #Pull in mass previous and iteration counter 
    global MassPrevious
    global i
    global model_number
    #Pull initial mass value for studio file
    MassCurrent = volume_from_onshape(DID,WID,EIDs)
    #Round to 10 decimals to remove rounding errors of last digits
    MassCurrent = round(MassCurrent, digits=10)
    #Display iteration counter
    println(i)
    #println(VolumeCurrent)
    #println(VolumePrevious)
    #if mass is not the smae, pull step file from onshape
    if MassCurrent != MassPrevious
        #Download as step to the designated folder on PC. Increment number for step file each time a new one is downloaded
        export_partstudio(DID, WID, EIDa;
            format="STEP",
            output_path=joinpath(folder, "$(name)$(model_number).step"))
        #Increment model counter to save each individually
        model_number = model_number + 1
        #Display pull if step file pulled
        println("Pull")
        #Set previous mass to current to check for new changes
        MassPrevious=MassCurrent
    else
        #If no change in mass detected, display no pull
        println("No Pull")
    end
    #Wait 0.25 seconds then iterate the counter and run again
    sleep(1)
    i = i + 1
    #add how long it took to run loop to timer
    global timer += time() - timer_current
end 
println(timer)