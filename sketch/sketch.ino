#include <Arduino_RouterBridge.h>

const int pin = 8;
unsigned long buzzerOffAt = 0;
bool buzzerOn = false;

void buzzer(int durationMs) {
  digitalWrite(pin, HIGH);
  buzzerOffAt = millis() + durationMs;
  buzzerOn = true;
  Monitor.print("Pin ");
  Monitor.print(pin);
  Monitor.print(" HIGH (");
  Monitor.print(durationMs);
  Monitor.println(" ms)");
}

void setup() {
  Monitor.begin(115200);
  pinMode(pin, OUTPUT);
  digitalWrite(pin, LOW);
  Monitor.print("Pin ");
  Monitor.print(pin);
  Monitor.println(" LOW (inicio)");
  Bridge.begin();
  Bridge.provide("buzzer", buzzer);
}

void loop() {
  // Apagado no bloqueante cuando vence el tiempo
  if (buzzerOn && (long)(millis() - buzzerOffAt) >= 0) {
    digitalWrite(pin, LOW);
    buzzerOn = false;
    Monitor.print("Pin ");
    Monitor.print(pin);
    Monitor.println(" LOW");
  }
}
