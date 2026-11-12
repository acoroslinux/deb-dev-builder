// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick 2.12
import calamares.slideshow 1.0

Slide {
    id: page
    objectName: "featureSlide"
    x: 0
    y: 0
    width: parent.width
    height: parent.height
    property string category: ""
    property string heading: ""
    property string description: ""
    property string detail: ""
    property url artwork: ""
    property color accent: "#67e8f9"
    readonly property bool compact: width < 650 || height < 390
    readonly property int inset: compact ? 26 : 38
    opacity: visible ? 1 : 0
    Behavior on opacity { NumberAnimation { duration: 450 } }

    Rectangle {
        anchors.fill: parent
        color: "#101a2c"
    }
    Image {
        anchors.fill: parent
        source: page.artwork
        fillMode: Image.PreserveAspectCrop
        opacity: 0.65
        asynchronous: true
    }
    Rectangle {
        anchors.fill: parent
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0; color: "#fc101a2c" }
            GradientStop { position: 0.55; color: "#ed101a2c" }
            GradientStop { position: 1; color: "#70101a2c" }
        }
    }
    Column {
        id: copy
        objectName: "slideCopy"
        anchors.left: parent.left
        anchors.leftMargin: page.inset
        anchors.verticalCenter: parent.verticalCenter
        anchors.verticalCenterOffset: -12
        width: parent.width - page.inset * 2
        spacing: page.compact ? 14 : 20
        Text {
            text: page.category
            color: page.accent
            font.pixelSize: 11
            font.bold: true
            font.letterSpacing: 2.1
            textFormat: Text.PlainText
        }
        Text {
            objectName: "slideHeading"
            width: parent.width * (page.compact ? 1 : 0.85)
            text: page.heading
            color: "#f5f8ff"
            font.family: "sans-serif"
            font.pixelSize: page.compact ? 27 : 36
            font.bold: true
            lineHeight: 1.1
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
        }
        Text {
            objectName: "slideDescription"
            width: parent.width * (page.compact ? 1 : 0.82)
            text: page.description
            color: "#c1cfe3"
            font.family: "sans-serif"
            font.pixelSize: page.compact ? 14 : 16
            lineHeight: 1.25
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
        }
        Rectangle {
            width: 36
            height: 2
            color: page.accent
            opacity: 0.7
        }
        Text {
            width: parent.width
            text: page.detail
            color: "#94aac7"
            font.pixelSize: 11
            font.letterSpacing: 0.4
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
        }
    }
}
