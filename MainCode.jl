"""
Main program for checking for changes and downloading step files when seen
"""

import os
import time
from datetime import datetime

# Base directory location on PC
base_loc = "E:/Onshape test"

# Pull current date/time
current_datetime = datetime.now()
current_datetime_str = current_datetime.strftime("%y_%m_%d_%H_%M")

# Set folder path to the base location with a new folder labelled date and time of start of experiment
folder = os.path.join(base_loc, current_datetime_str)

# If folder doesn't exist, create it
if not os.path.isdir(folder):
    os.makedirs(folder)

# Setup variables for Onshape CAD
# DocumentID
DID = "6c0d2b9b726f93f6e30525f2"

# WorkspaceID
WID = "e60b006b496816beb3fba67d"

# Assembly ID for studio
EIDs = "045a083a25ff8005c749039f"

# Assembly ID for Assembly
EIDa = "9524668d3f639b4055de0e2b"

# Pull assembly file
name = File_Name(DID)

# Setup Iteration counter, initial mass measurement, model iteration and timer
i = 0
MassPrevious = None
model_number = 0
timer = 0.0

# While loop to setup number of iterations or how much time to run (timer <= max time in seconds)
while timer <= 10:
    timer_current = time.time()
    
    # Pull in mass previous and iteration counter
    # (already global in Python within function scope)
    
    # Pull initial mass value for studio file
    MassCurrent = volume_from_onshape(DID, WID, EIDs)
    
    # Round to 10 decimals to remove rounding errors of last digits
    MassCurrent = round(MassCurrent, 10)
    
    # Display iteration counter
    print(i)
    # print(VolumeCurrent)
    # print(VolumePrevious)
    
    # If mass is not the same, pull step file from onshape
    if MassCurrent != MassPrevious:
        # Download as step to the designated folder on PC.
        # Increment number for step file each time a new one is downloaded
        export_partstudio(
            DID,
            WID,
            EIDa,
            format="STEP",
            output_path=os.path.join(folder, f"{name}{model_number}.step")
        )
        
        # Increment model counter to save each individually
        model_number = model_number + 1
        
        # Display pull if step file pulled
        print("Pull")
        
        # Set previous mass to current to check for new changes
        MassPrevious = MassCurrent
    else:
        # If no change in mass detected, display no pull
        print("No Pull")
    
    # Wait 1 second then iterate the counter and run again
    time.sleep(1)
    i = i + 1
    
    # Add how long it took to run loop to timer
    timer += time.time() - timer_current

print(timer)
