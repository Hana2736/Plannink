#!/bin/bash

# A script to manage the TrainData zip archive and its split chunks.

ACTION=$1

# Check for required tools
for tool in zip unzip split cat; do
    if ! command -v $tool &> /dev/null; then
        echo "❌ Error: $tool is not installed."
        exit 1
    fi
done

if [ "$ACTION" == "zip" ]; then
    echo "📦 Zipping TrainData directory..."
    if [ -d "TrainData" ]; then
        # Use -q for quiet, -r for recursive
        zip -rq TrainData.zip TrainData
        echo "✂️ Splitting TrainData.zip into 1.9GB chunks..."
        split -b 1900M -d -a 4 TrainData.zip TrainData.zip.
        rm TrainData.zip
        echo "✅ TrainData has been zipped and split into chunks."
    else
        echo "❌ Error: TrainData directory not found."
        exit 1
    fi
elif [ "$ACTION" == "unzip" ]; then
    if [ -d "TrainData" ]; then
        echo "ℹ️ TrainData directory already exists. Skipping unzip."
        exit 0
    fi

    echo "拼接 Reassembling TrainData.zip from chunks..."
    if [ -f "TrainData.zip.0000" ]; then
        cat TrainData.zip.* > TrainData.zip
        echo "🔓 Unzipping TrainData.zip..."
        unzip -q TrainData.zip
        # We keep the reassembled TrainData.zip for the training script to see, 
        # or we could remove it to save space. 
        # For now, let's remove it after success to save disk space, 
        # since the chunks are the permanent storage.
        rm TrainData.zip
        echo "✅ TrainData has been restored."
    else
        echo "❌ Error: TrainData.zip chunks not found."
        exit 1
    fi
else
    echo "Usage: $0 {zip|unzip}"
    exit 1
fi
