// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick 2.12
import calamares.slideshow 1.0

Presentation {
    id: presentation
    objectName: "debDevSlideshow"
    mouseNavigation: false
    arrowNavigation: true
    loopSlides: true
    allowDelay: false

    function onActivate() {
        currentSlide = 0
        activatedInCalamares = true
    }
    function onLeave() {
        activatedInCalamares = false
        advanceTimer.stop()
    }
    Timer {
        id: advanceTimer
        objectName: "slideTimer"
        interval: 9000
        running: presentation.activatedInCalamares && presentation.visible
        repeat: true
        onTriggered: presentation.goToNextSlide()
    }

    FeatureSlide {
        category: qsTr("WELCOME TO DEB-DEV MODERN")
        heading: qsTr("A familiar foundation.\nA fresh start.")
        description: qsTr("Built on Debian and Devuan, with the desktop you chose. A system you can explore, understand and make your own.")
        detail: qsTr("OPEN SOURCE  /  YOUR CHOICE  /  YOUR SYSTEM")
        artwork: "slide_future.jpg"
    }
    FeatureSlide {
        category: qsTr("A DESKTOP THAT FITS")
        heading: qsTr("Make room for your\nway of working.")
        description: qsTr("Arrange your workspace, choose your favourite applications and adjust the little details. Your desktop is a starting point, not a limit.")
        detail: qsTr("WORK  /  CREATE  /  EXPLORE")
        artwork: "slide_speed.jpg"
        accent: "#a6b6ff"
    }
    FeatureSlide {
        category: qsTr("TOOLS FOR YOUR NEXT IDEA")
        heading: qsTr("Find what you need.\nMake something new.")
        description: qsTr("Explore the software available in your distribution's repositories. Use your package manager to add the tools you need and keep them up to date.")
        detail: qsTr("APPLICATIONS  /  MEDIA  /  DEVELOPMENT")
        artwork: "slide_future.jpg"
        accent: "#bfa6ff"
    }
    FeatureSlide {
        category: qsTr("GOOD HABITS, STRONG FOUNDATIONS")
        heading: qsTr("Take care of what\nmatters to you.")
        description: qsTr("Keep your system updated, use strong passwords and back up your files. If you chose disk encryption, keep your passphrase somewhere safe.")
        detail: qsTr("UPDATES  /  PASSWORDS  /  BACKUPS")
        artwork: "slide_security.jpg"
    }
    FeatureSlide {
        category: qsTr("YOUR NEXT CHAPTER")
        heading: qsTr("Your system.\nYour way.")
        description: qsTr("When installation finishes, follow the restart instructions and remove the installation medium when prompted. Then sign in and make yourself at home.")
        detail: qsTr("THANK YOU FOR CHOOSING DEB-DEV MODERN")
        artwork: "slide_speed.jpg"
        accent: "#a6b6ff"
    }

    Rectangle {
        z: 10
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: 52
        color: "#ed0c1525"
        Text {
            anchors.left: parent.left
            anchors.leftMargin: 26
            anchors.verticalCenter: parent.verticalCenter
            text: qsTr("Discover your new system")
            color: "#97adc9"
            font.pixelSize: 11
        }
        Row {
            anchors.right: parent.right
            anchors.rightMargin: 26
            anchors.verticalCenter: parent.verticalCenter
            spacing: 8
            Repeater {
                model: presentation.slides.length
                Rectangle {
                    width: 20
                    height: 20
                    radius: 10
                    color: "transparent"
                    Accessible.role: Accessible.Button
                    Accessible.name: qsTr("Show slide %1").arg(index + 1)
                    Accessible.onPressAction: presentation.currentSlide = index
                    Rectangle {
                        anchors.centerIn: parent
                        width: 7
                        height: 7
                        radius: 4
                        color: presentation.currentSlide === index ? "#67e8f9" : "#52627c"
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            presentation.currentSlide = index
                            if (presentation.activatedInCalamares)
                                advanceTimer.restart()
                        }
                    }
                }
            }
        }
    }
}
