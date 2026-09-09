import QtQuick 2.8
import calamares.slideshow 1.0

Presentation {
    id: presentation

    Timer {
        id: advanceTimer
        interval: 5000
        running: true
        repeat: true
        onTriggered: presentation.goToNextSlide()
    }

    Slide {
        Image {
            anchors.fill: parent
            source: "slide_speed.png"
            fillMode: Image.PreserveAspectCrop
        }
        Rectangle {
            anchors.bottom: parent.bottom
            width: parent.width
            height: 80
            color: "#80000000"
            Text {
                anchors.centerIn: parent
                text: "Lightning Fast Performance"
                color: "white"
                font.pixelSize: 32
                font.bold: true
            }
        }
    }
    Slide {
        Image {
            anchors.fill: parent
            source: "slide_security.png"
            fillMode: Image.PreserveAspectCrop
        }
        Rectangle {
            anchors.bottom: parent.bottom
            width: parent.width
            height: 80
            color: "#80000000"
            Text {
                anchors.centerIn: parent
                text: "Built for Security and Stability"
                color: "white"
                font.pixelSize: 32
                font.bold: true
            }
        }
    }
    Slide {
        Image {
            anchors.fill: parent
            source: "slide_future.png"
            fillMode: Image.PreserveAspectCrop
        }
        Rectangle {
            anchors.bottom: parent.bottom
            width: parent.width
            height: 80
            color: "#80000000"
            Text {
                anchors.centerIn: parent
                text: "The Future of Computing"
                color: "white"
                font.pixelSize: 32
                font.bold: true
            }
        }
    }
}
