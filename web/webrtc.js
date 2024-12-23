const button = document.getElementById('dc_close');
const outputDiv = document.getElementById('dc_close_feedback');

const lidars = ["LIDAR1", "LIDAR2"];
// const lidars = ["LIDAR1"];
lidarRead = [...lidars];
allData = [];

let xValues = [];
let yValues = [];

const trace = {
    x: xValues,
    y: yValues,
    mode: 'markers+lines',
    type: 'scatter'
};

const layout = {
    title: 'OKDO LD06 to WebRTC Data Channel',
    xaxis: { title: 'X' },
    yaxis: { title: 'Y' }
};

Plotly.newPlot('graph', [trace], layout);

function updateGraph(newPoints) {
    xValues = newPoints.map(point => point[0]);
    yValues = newPoints.map(point => point[1]);

    Plotly.react('graph', [{ x: xValues, y: yValues, mode: 'markers+lines', type: 'scatter' }], layout);
}

var pc = null;
var dc = null;

async function startConnection() {

    pc = new RTCPeerConnection();
    dc = pc.createDataChannel("lidar", { negotiated: true, ordered: true, id: 2 });

    dc.addEventListener('open', () => {
        console.log("Data channel opened");
    });
    dc.addEventListener('message', (evt) => {
        const newData = JSON.parse(evt.data);
        lidarName = Object.keys(newData)[0];
        lidarData = Object.values(newData)[0];
        if (lidarRead.includes(lidarName)) {
            lidarRead = lidarRead.filter(item => item !== lidarName);
            allData = [...allData, ...lidarData];
            if(lidarRead.length === 0) {
                updateGraph(allData);
                allData = [];
                lidarRead = [...lidars];
            }
        } else {
            console.log(`${lidarName} message rejected`);
        }
    });
    dc.addEventListener('close', () => {
        console.log("Data channel closed");
        outputDiv.textContent = "CLOSED";
    });
    
    negotiate();
}

function negotiate() {
    return pc.createOffer().then((offer) => {
        return pc.setLocalDescription(offer);
    }).then(() => {
        var offer = pc.localDescription;
        var codec;
    
        return fetch('/wrtc', {
            body: JSON.stringify({
                sdp: offer.sdp,
                type: offer.type
            }),
            headers: {
                'Content-Type': 'application/json'
            },
            method: 'POST'
        });
    }).then((response) => {
        return response.json();
    }).then((answer) => {
        console.log(answer.sdp);
        return pc.setRemoteDescription(answer);
    }).catch((e) => {
        alert(e);
    });
}

window.onload = () => {
    startConnection();
    button.addEventListener('click', () => {
        dc.close();
    });
};
