// Interop harness: an eebus-go CEM (EVSECC, EVCC, EVCEM, OPEV, OSCEV), a spine-go
// based EVSE with an EV entity, an eebus-go controllable system (LPC, MPC) or an
// eebus-go Energy Guard (LPC, MPC monitoring), to test pyeebus (and tools built on it)
// against the Go reference. It only accepts connections from the trusted SKI.
//
// Output lines (stdout) are meant for the tests: SKI, READY, CONNECTED, EVENT, ...
package main

import (
	"bufio"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"flag"
	"fmt"
	"os"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"time"
	"unsafe"

	"github.com/enbility/eebus-go/api"
	"github.com/enbility/eebus-go/service"
	ucapi "github.com/enbility/eebus-go/usecases/api"
	"github.com/enbility/eebus-go/usecases/cem/evcc"
	"github.com/enbility/eebus-go/usecases/cem/evcem"
	"github.com/enbility/eebus-go/usecases/cem/evsecc"
	"github.com/enbility/eebus-go/usecases/cem/opev"
	"github.com/enbility/eebus-go/usecases/cem/oscev"
	cslpc "github.com/enbility/eebus-go/usecases/cs/lpc"
	eglpc "github.com/enbility/eebus-go/usecases/eg/lpc"
	mampc "github.com/enbility/eebus-go/usecases/ma/mpc"
	mumpc "github.com/enbility/eebus-go/usecases/mu/mpc"
	shipapi "github.com/enbility/ship-go/api"
	"github.com/enbility/ship-go/cert"
	"github.com/enbility/ship-go/mdns"
	spineapi "github.com/enbility/spine-go/api"
	"github.com/enbility/spine-go/model"
	"github.com/enbility/spine-go/spine"
	"github.com/enbility/spine-go/util"
)

var out sync.Mutex

func say(format string, args ...any) {
	out.Lock()
	defer out.Unlock()
	fmt.Printf(format+"\n", args...)
}

type harness struct {
	svc     *service.Service
	evsecc  *evsecc.EVSECC
	evcc    *evcc.EVCC
	evcem   *evcem.EVCEM
	opev    *opev.OPEV
	oscev   *oscev.OSCEV
	wrote   bool
	evse    spineapi.EntityLocalInterface
	ev      spineapi.EntityLocalInterface
	verbose bool
	cslpc   *cslpc.LPC
	mumpc   *mumpc.MPC
	eglpc   *eglpc.LPC
	mampc   *mampc.MPC
	egEVSE  spineapi.EntityRemoteInterface
	egLimit float64
}

// --- service reader / logging -------------------------------------------------------

func (h *harness) RemoteServiceConnected(_ api.ServiceInterface, id shipapi.ServiceIdentity) {
	say("CONNECTED %s", id.SKI)
}
func (h *harness) RemoteServiceDisconnected(_ api.ServiceInterface, id shipapi.ServiceIdentity) {
	say("DISCONNECTED %s", id.SKI)
}
func (h *harness) VisibleRemoteMdnsServicesUpdated(_ api.ServiceInterface, _ []shipapi.RemoteMdnsService) {
}
func (h *harness) ServiceUpdated(_ shipapi.ServiceIdentity) {}
func (h *harness) ServicePairingDetailUpdate(_ shipapi.ServiceIdentity, _ *shipapi.ConnectionStateDetail) {
}
func (h *harness) ServiceAutoTrusted(_ api.ServiceInterface, _ shipapi.ServiceIdentity) {}
func (h *harness) ServiceAutoTrustFailed(_ api.ServiceInterface, _ shipapi.ServiceIdentity, _ error) {
}
func (h *harness) ServiceAutoTrustRemoved(_ api.ServiceInterface, _ shipapi.ServiceIdentity, _ string) {
}

func (h *harness) log(args ...any) {
	if h.verbose {
		fmt.Fprintln(os.Stderr, args...)
	}
}
func (h *harness) logf(f string, args ...any) {
	if h.verbose {
		fmt.Fprintf(os.Stderr, f+"\n", args...)
	}
}
func (h *harness) Trace(args ...any)            { h.log(args...) }
func (h *harness) Tracef(f string, args ...any) { h.logf(f, args...) }
func (h *harness) Debug(args ...any)            { h.log(args...) }
func (h *harness) Debugf(f string, args ...any) { h.logf(f, args...) }
func (h *harness) Info(args ...any)             { h.log(args...) }
func (h *harness) Infof(f string, args ...any)  { h.logf(f, args...) }
func (h *harness) Error(args ...any)            { h.log(args...) }
func (h *harness) Errorf(f string, args ...any) { h.logf(f, args...) }

// stub mDNS provider: the tests connect directly, no multicast needed
type stubProvider struct{}

func (s *stubProvider) Start(_ shipapi.PairingMode, _ bool, _ shipapi.MdnsResolveCB) bool {
	return true
}
func (s *stubProvider) Shutdown() {}
func (s *stubProvider) AnnounceService(_, _ string, _ int, _ []string) (string, error) {
	return "stub", nil
}
func (s *stubProvider) UnannounceService(_ string) error { return nil }

// the eebus-go service keeps its mDNS manager private
func injectMdnsProvider(svc *service.Service) {
	field := reflect.ValueOf(svc).Elem().FieldByName("mdns")
	value := reflect.NewAt(field.Type(), unsafe.Pointer(field.UnsafeAddr())).Elem().Interface()
	manager, ok := value.(*mdns.MdnsManager)
	if !ok {
		panic("unexpected mDNS type")
	}
	manager.SetMdnsProvider(&stubProvider{})
}

// --- CEM ---------------------------------------------------------------------------

func floats(v []float64) string {
	s := make([]string, len(v))
	for i, f := range v {
		s[i] = strconv.FormatFloat(f, 'f', -1, 64)
	}
	return strings.Join(s, ",")
}

func (h *harness) cemEvent(_ string, _ spineapi.DeviceRemoteInterface, entity spineapi.EntityRemoteInterface, event api.EventType) {
	etype := ""
	if entity != nil {
		etype = string(entity.EntityType())
	}
	say("EVENT %s %s", event, etype)
	switch event {
	case evsecc.DataUpdateManufacturerData:
		if d, err := h.evsecc.ManufacturerData(entity); err == nil {
			say("EVSE_MANUFACTURER %s", d.DeviceName)
		}
	case evcc.DataUpdateCommunicationStandard:
		if s, err := h.evcc.CommunicationStandard(entity); err == nil {
			say("EVCC_STANDARD %s", s)
		}
	case evcem.DataUpdateCurrentPerPhase:
		if c, err := h.evcem.CurrentPerPhase(entity); err == nil {
			say("EVCEM_CURRENT %s", floats(c))
		}
	case evcem.DataUpdateEnergyCharged:
		if e, err := h.evcem.EnergyCharged(entity); err == nil {
			say("EVCEM_ENERGY %s", floats([]float64{e}))
		}
	case opev.DataUpdateLimit, opev.DataUpdateCurrentLimits:
		limits, err := h.opev.LoadControlLimits(entity)
		if err != nil {
			return
		}
		values := []float64{}
		for _, l := range limits {
			values = append(values, l.Value)
		}
		say("OPEV_LIMITS %s", floats(values))
		// eebus-go may report fewer phases while data is still arriving
		if !h.wrote && len(limits) == 3 {
			h.wrote = true
			write := []ucapi.LoadLimitsPhase{}
			for _, l := range limits {
				write = append(write, ucapi.LoadLimitsPhase{Phase: l.Phase, IsActive: true, Value: 10})
			}
			_, err := h.opev.WriteLoadControlLimits(entity, write, func(r model.ResultDataType, _ model.MsgCounterType) {
				say("WRITE_RESULT %d", *r.ErrorNumber)
			})
			if err != nil {
				say("WRITE_ERROR %s", err)
			}
		}
	}
}

func (h *harness) setupCEM() {
	local := h.svc.LocalDevice().EntityForType(model.EntityTypeTypeCEM)
	h.evsecc = evsecc.NewEVSECC(local, h.cemEvent)
	h.evcc = evcc.NewEVCC(h.svc, local, h.cemEvent)
	h.evcem = evcem.NewEVCEM(h.svc, local, h.cemEvent)
	h.opev = opev.NewOPEV(local, h.cemEvent)
	h.oscev = oscev.NewOSCEV(local, h.cemEvent)
	for _, uc := range []api.UseCaseInterface{h.evsecc, h.evcc, h.evcem, h.opev, h.oscev} {
		if err := h.svc.AddUseCase(uc); err != nil {
			panic(err)
		}
	}
}

// --- Energy Guard (LPC) and Monitoring Appliance (MPC): a HEMS like Solar Manager -----

func (h *harness) egEvent(_ string, _ spineapi.DeviceRemoteInterface, entity spineapi.EntityRemoteInterface, event api.EventType) {
	switch event {
	case eglpc.UseCaseSupportUpdate:
		if entity != nil && entity.EntityType() == model.EntityTypeTypeEVSE && h.egEVSE == nil {
			h.egEVSE = entity
			say("EG_EVSE")
		}
	case eglpc.DataUpdateLimit:
		if l, err := h.eglpc.ConsumptionLimit(entity); err == nil {
			say("EG_LIMIT %s %t", floats([]float64{l.Value}), l.IsActive)
		}
	case eglpc.DataUpdateFailsafeConsumptionActivePowerLimit:
		if v, err := h.eglpc.FailsafeConsumptionActivePowerLimit(entity); err == nil {
			say("EG_FAILSAFE %s", floats([]float64{v}))
		}
	case eglpc.DataUpdateHeartbeat:
		say("EG_HEARTBEAT")
	case mampc.DataUpdatePower:
		if v, err := h.mampc.Power(entity); err == nil {
			say("MPC_POWER %s", floats([]float64{v}))
		}
	}
}

func (h *harness) setupEG() {
	local := h.svc.LocalDevice().EntityForType(model.EntityTypeTypeCEM)
	h.eglpc = eglpc.NewLPC(local, h.egEvent)
	h.mampc = mampc.NewMPC(local, h.egEvent)
	for _, uc := range []api.UseCaseInterface{h.eglpc, h.mampc} {
		if err := h.svc.AddUseCase(uc); err != nil {
			panic(err)
		}
	}
	h.eglpc.StartHeartbeat()
}

// write an LPC limit to the EVSE found via use case discovery
func (h *harness) egWrite(value float64, active bool, duration time.Duration) {
	if h.egEVSE == nil {
		say("WRITE_ERROR no EVSE")
		return
	}
	limit := ucapi.LoadLimit{Value: value, IsActive: active, Duration: duration}
	_, err := h.eglpc.WriteConsumptionLimit(h.egEVSE, limit, func(r model.ResultDataType, _ model.MsgCounterType) {
		say("WRITE_RESULT %d", *r.ErrorNumber)
	})
	if err != nil {
		say("WRITE_ERROR %s", err)
	}
}

// --- EVSE --------------------------------------------------------------------------

func set(f spineapi.FeatureLocalInterface, function model.FunctionType, write bool, target any, js string) {
	f.AddFunctionType(function, true, write)
	if err := json.Unmarshal([]byte(js), target); err != nil {
		panic(fmt.Sprintf("%s: %v", function, err))
	}
	f.SetData(function, target)
}

func (h *harness) setupEVSE() {
	h.evse = h.svc.LocalDevice().EntityForType(model.EntityTypeTypeEVSE)
	f := h.evse.GetOrAddFeature(model.FeatureTypeTypeDeviceClassification, model.RoleTypeServer)
	set(f, model.FunctionTypeDeviceClassificationManufacturerData, false, &model.DeviceClassificationManufacturerDataType{},
		`{"deviceName":"GoEVSE","brandName":"enbility"}`)
	f = h.evse.GetOrAddFeature(model.FeatureTypeTypeDeviceDiagnosis, model.RoleTypeServer)
	set(f, model.FunctionTypeDeviceDiagnosisStateData, false, &model.DeviceDiagnosisStateDataType{},
		`{"operatingState":"normalOperation"}`)
	h.evse.AddUseCaseSupport(model.UseCaseActorTypeEVSE, model.UseCaseNameTypeEVSECommissioningAndConfiguration,
		"1.0.1", "release", true, []model.UseCaseScenarioSupportType{1, 2})
	h.svc.LocalDevice().Events().Subscribe(h)
}

func (h *harness) plug() {
	if h.ev != nil {
		return
	}
	device := h.svc.LocalDevice()
	ev := spine.NewEntityLocal(device, model.EntityTypeTypeEV, []model.AddressEntityType{1, 1}, 4*time.Second)
	f := ev.GetOrAddFeature(model.FeatureTypeTypeDeviceConfiguration, model.RoleTypeServer)
	set(f, model.FunctionTypeDeviceConfigurationKeyValueDescriptionListData, false, &model.DeviceConfigurationKeyValueDescriptionListDataType{},
		`{"deviceConfigurationKeyValueDescriptionData":[{"keyId":1,"keyName":"communicationsStandard","valueType":"string"}]}`)
	set(f, model.FunctionTypeDeviceConfigurationKeyValueListData, false, &model.DeviceConfigurationKeyValueListDataType{},
		`{"deviceConfigurationKeyValueData":[{"keyId":1,"value":{"string":"iso15118-2ed1"}}]}`)
	f = ev.GetOrAddFeature(model.FeatureTypeTypeDeviceDiagnosis, model.RoleTypeServer)
	set(f, model.FunctionTypeDeviceDiagnosisStateData, false, &model.DeviceDiagnosisStateDataType{},
		`{"operatingState":"normalOperation"}`)
	f = ev.GetOrAddFeature(model.FeatureTypeTypeElectricalConnection, model.RoleTypeServer)
	set(f, model.FunctionTypeElectricalConnectionDescriptionListData, false, &model.ElectricalConnectionDescriptionListDataType{},
		`{"electricalConnectionDescriptionData":[{"electricalConnectionId":0,"powerSupplyType":"ac","acConnectedPhases":3,"positiveEnergyDirection":"consume"}]}`)
	set(f, model.FunctionTypeElectricalConnectionParameterDescriptionListData, false, &model.ElectricalConnectionParameterDescriptionListDataType{},
		`{"electricalConnectionParameterDescriptionData":[
		 {"electricalConnectionId":0,"parameterId":1,"measurementId":1,"voltageType":"ac","acMeasuredPhases":"a"},
		 {"electricalConnectionId":0,"parameterId":2,"measurementId":2,"voltageType":"ac","acMeasuredPhases":"b"},
		 {"electricalConnectionId":0,"parameterId":3,"measurementId":3,"voltageType":"ac","acMeasuredPhases":"c"},
		 {"electricalConnectionId":0,"parameterId":4,"scopeType":"acPowerTotal"}]}`)
	set(f, model.FunctionTypeElectricalConnectionPermittedValueSetListData, false, &model.ElectricalConnectionPermittedValueSetListDataType{},
		`{"electricalConnectionPermittedValueSetData":[
		 {"electricalConnectionId":0,"parameterId":1,"permittedValueSet":[{"value":[{"number":0,"scale":0}],"range":[{"min":{"number":6,"scale":0},"max":{"number":16,"scale":0}}]}]},
		 {"electricalConnectionId":0,"parameterId":2,"permittedValueSet":[{"value":[{"number":0,"scale":0}],"range":[{"min":{"number":6,"scale":0},"max":{"number":16,"scale":0}}]}]},
		 {"electricalConnectionId":0,"parameterId":3,"permittedValueSet":[{"value":[{"number":0,"scale":0}],"range":[{"min":{"number":6,"scale":0},"max":{"number":16,"scale":0}}]}]},
		 {"electricalConnectionId":0,"parameterId":4,"permittedValueSet":[{"value":[{"number":0,"scale":0}],"range":[{"min":{"number":4140,"scale":0},"max":{"number":11040,"scale":0}}]}]}]}`)
	f = ev.GetOrAddFeature(model.FeatureTypeTypeMeasurement, model.RoleTypeServer)
	set(f, model.FunctionTypeMeasurementDescriptionListData, false, &model.MeasurementDescriptionListDataType{},
		`{"measurementDescriptionData":[
		 {"measurementId":1,"measurementType":"current","commodityType":"electricity","unit":"A","scopeType":"acCurrent"},
		 {"measurementId":2,"measurementType":"current","commodityType":"electricity","unit":"A","scopeType":"acCurrent"},
		 {"measurementId":3,"measurementType":"current","commodityType":"electricity","unit":"A","scopeType":"acCurrent"},
		 {"measurementId":4,"measurementType":"energy","commodityType":"electricity","unit":"Wh","scopeType":"charge"}]}`)
	set(f, model.FunctionTypeMeasurementListData, false, &model.MeasurementListDataType{},
		`{"measurementData":[
		 {"measurementId":1,"valueType":"value","value":{"number":0}},
		 {"measurementId":2,"valueType":"value","value":{"number":0}},
		 {"measurementId":3,"valueType":"value","value":{"number":0}},
		 {"measurementId":4,"valueType":"value","value":{"number":0}}]}`)
	f = ev.GetOrAddFeature(model.FeatureTypeTypeLoadControl, model.RoleTypeServer)
	set(f, model.FunctionTypeLoadControlLimitDescriptionListData, false, &model.LoadControlLimitDescriptionListDataType{},
		`{"loadControlLimitDescriptionData":[
		 {"limitId":1,"limitType":"maxValueLimit","limitCategory":"obligation","measurementId":1,"unit":"A","scopeType":"overloadProtection"},
		 {"limitId":2,"limitType":"maxValueLimit","limitCategory":"obligation","measurementId":2,"unit":"A","scopeType":"overloadProtection"},
		 {"limitId":3,"limitType":"maxValueLimit","limitCategory":"obligation","measurementId":3,"unit":"A","scopeType":"overloadProtection"}]}`)
	set(f, model.FunctionTypeLoadControlLimitListData, true, &model.LoadControlLimitListDataType{},
		`{"loadControlLimitData":[
		 {"limitId":1,"isLimitChangeable":true,"isLimitActive":false,"value":{"number":16,"scale":0}},
		 {"limitId":2,"isLimitChangeable":true,"isLimitActive":false,"value":{"number":16,"scale":0}},
		 {"limitId":3,"isLimitChangeable":true,"isLimitActive":false,"value":{"number":16,"scale":0}}]}`)
	scen := func(n ...model.UseCaseScenarioSupportType) []model.UseCaseScenarioSupportType { return n }
	ev.AddUseCaseSupport(model.UseCaseActorTypeEV, model.UseCaseNameTypeEVCommissioningAndConfiguration, "1.0.1", "release", true, scen(1, 2, 3, 8))
	ev.AddUseCaseSupport(model.UseCaseActorTypeEV, model.UseCaseNameTypeMeasurementOfElectricityDuringEVCharging, "1.0.1", "release", true, scen(1, 3))
	ev.AddUseCaseSupport(model.UseCaseActorTypeEV, model.UseCaseNameTypeOverloadProtectionByEVChargingCurrentCurtailment, "1.0.1", "release", true, scen(1, 2, 3))
	device.AddEntity(ev)
	h.ev = ev
	say("PLUGGED")
}

func (h *harness) unplug() {
	if h.ev == nil {
		return
	}
	h.svc.LocalDevice().RemoveEntity(h.ev)
	h.ev = nil
	say("UNPLUGGED")
}

func (h *harness) measure(current, energy float64) {
	if h.ev == nil {
		return
	}
	f := h.ev.FeatureOfTypeAndRole(model.FeatureTypeTypeMeasurement, model.RoleTypeServer)
	data := &model.MeasurementListDataType{}
	js := fmt.Sprintf(`{"measurementData":[
	 {"measurementId":1,"valueType":"value","value":{"number":%d}},
	 {"measurementId":2,"valueType":"value","value":{"number":%d}},
	 {"measurementId":3,"valueType":"value","value":{"number":%d}},
	 {"measurementId":4,"valueType":"value","value":{"number":%d}}]}`, int(current), int(current), int(current), int(energy))
	_ = json.Unmarshal([]byte(js), data)
	f.SetData(model.FunctionTypeMeasurementListData, data)
	say("MEASURED")
}

// --- LPC / MPC (controllable system, monitored unit) ---------------------------------

func (h *harness) lpcEvent(_ string, _ spineapi.DeviceRemoteInterface, _ spineapi.EntityRemoteInterface, event api.EventType) {
	switch event {
	case cslpc.LimitWriteApprovalRequired:
		for counter, limit := range h.cslpc.PendingConsumptionLimits() {
			say("LPC_LIMIT %s %t %s", floats([]float64{limit.Value}), limit.IsActive, limit.Duration)
			h.cslpc.ApproveOrDenyConsumptionLimit(counter, true, "")
		}
	case cslpc.ConfigurationWriteApprovalRequired:
		for counter, configs := range h.cslpc.PendingDeviceConfigurations() {
			for _, c := range configs {
				if c.Value.ScaledNumber != nil {
					say("LPC_CONFIG %s %s", c.KeyName, floats([]float64{c.Value.ScaledNumber.GetValue()}))
				} else if c.Value.Duration != nil {
					d, _ := c.Value.Duration.GetTimeDuration()
					say("LPC_CONFIG %s %s", c.KeyName, d)
				}
			}
			h.cslpc.ApproveOrDenyDeviceConfiguration(counter, true, "")
		}
	case cslpc.DataUpdateHeartbeat:
		say("LPC_HEARTBEAT")
	}
}

func (h *harness) setupLPC() {
	entity := h.svc.LocalDevice().EntityForType(model.EntityTypeTypeEVSE)
	h.cslpc = cslpc.NewLPC(entity, h.lpcEvent)
	measured := util.Ptr(model.MeasurementValueSourceTypeMeasuredValue)
	perPhase := mumpc.PhaseMeasurementSourceMap{"a": measured, "b": measured, "c": measured}
	var err error
	h.mumpc, err = mumpc.NewMPC(entity, nil,
		&mumpc.MonitorPowerConfig{ConnectedPhases: "abc", ValueSourceTotal: measured, ValueSourcePerPhase: perPhase},
		&mumpc.MonitorEnergyConfig{ValueSourceConsumption: measured},
		&mumpc.MonitorCurrentConfig{ValueSourcePerPhase: perPhase},
		&mumpc.MonitorVoltageConfig{ValueSourcePerPhase: perPhase},
		&mumpc.MonitorFrequencyConfig{ValueSource: measured})
	if err != nil {
		panic(err)
	}
	for _, uc := range []api.UseCaseInterface{h.cslpc, h.mumpc} {
		if err := h.svc.AddUseCase(uc); err != nil {
			panic(err)
		}
	}
	_ = h.cslpc.SetConsumptionNominalMax(11000)
	_ = h.cslpc.SetConsumptionLimit(ucapi.LoadLimit{Value: 11000, IsChangeable: true, IsActive: false})
	_ = h.cslpc.SetFailsafeConsumptionActivePowerLimit(4200, true)
	_ = h.cslpc.SetFailsafeDurationMinimum(2*time.Hour, true)
	h.updatePower(3000)
}

func (h *harness) updatePower(total float64) {
	m := h.mumpc
	err := m.Update(
		m.UpdateDataPowerTotal(total, nil, nil),
		m.UpdateDataPowerPhaseA(total/3, nil, nil), m.UpdateDataPowerPhaseB(total/3, nil, nil), m.UpdateDataPowerPhaseC(total/3, nil, nil),
		m.UpdateDataEnergyConsumed(12345, nil, nil, nil, nil),
		m.UpdateDataCurrentPhaseA(total/3/230, nil, nil), m.UpdateDataCurrentPhaseB(total/3/230, nil, nil), m.UpdateDataCurrentPhaseC(total/3/230, nil, nil),
		m.UpdateDataVoltagePhaseA(230, nil, nil), m.UpdateDataVoltagePhaseB(231, nil, nil), m.UpdateDataVoltagePhaseC(232, nil, nil),
		m.UpdateDataFrequency(50, nil, nil),
	)
	if err != nil {
		say("UPDATE_ERROR %s", err)
		return
	}
	say("POWER %s", floats([]float64{total}))
}

// spine-go event handler (EVSE mode): report limit writes and subscriptions
func (h *harness) HandleEvent(payload spineapi.EventPayload) {
	if payload.EventType == spineapi.EventTypeSubscriptionChange && payload.ChangeType == spineapi.ElementChangeAdd {
		if payload.LocalFeature != nil {
			say("SUBSCRIBED %s", payload.LocalFeature.Type())
		}
	}
	if payload.EventType == spineapi.EventTypeBindingChange && payload.ChangeType == spineapi.ElementChangeAdd {
		if payload.LocalFeature != nil {
			say("BOUND %s", payload.LocalFeature.Type())
		}
	}
	if payload.EventType != spineapi.EventTypeDataChange || payload.CmdClassifier == nil ||
		*payload.CmdClassifier != model.CmdClassifierTypeWrite {
		return
	}
	if data, ok := payload.Data.(*model.LoadControlLimitListDataType); ok {
		for _, l := range data.LoadControlLimitData {
			say("LIMIT_WRITE %d %s %t", *l.LimitId, floats([]float64{l.Value.GetValue()}), *l.IsLimitActive)
		}
	}
}

func main() {
	mode := flag.String("mode", "cem", "cem, evse, lpc or eg")
	port := flag.Int("port", 4811, "listen port")
	trust := flag.String("trust", "", "remote SKI to trust")
	certOut := flag.String("cert-out", "", "write own certificate PEM here")
	verbose := flag.Bool("v", false, "log to stderr")
	flag.Parse()

	c, err := cert.CreateCertificate("t", "harness", "DE", "harness-"+*mode)
	if err != nil {
		panic(err)
	}
	x, _ := x509.ParseCertificate(c.Certificate[0])
	ski, _ := cert.SkiFromCertificate(x)
	if *certOut != "" {
		_ = os.WriteFile(*certOut, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: c.Certificate[0]}), 0o644)
	}
	say("SKI %s", ski)

	h := &harness{verbose: *verbose}
	deviceType, entity, category := model.DeviceTypeTypeEnergyManagementSystem, model.EntityTypeTypeCEM, shipapi.DeviceCategoryTypeEnergyManagementSystem
	if *mode == "evse" || *mode == "lpc" {
		deviceType, entity, category = model.DeviceTypeTypeChargingStation, model.EntityTypeTypeEVSE, shipapi.DeviceCategoryTypeEMobility
	}
	cfg, err := api.NewConfiguration("Demo", "Demo", "Harness", "1", []shipapi.DeviceCategoryType{category},
		deviceType, []model.EntityTypeType{entity}, *port, c, 4*time.Second, nil, nil)
	if err != nil {
		panic(err)
	}
	cfg.SetMdnsProviderSelection(mdns.MdnsProviderSelectionTestSetup)
	cfg.SetInterfaces([]string{"lo"})
	h.svc = service.NewService(cfg, h)
	h.svc.SetLogging(h)
	if err := h.svc.Setup(); err != nil {
		panic(err)
	}
	injectMdnsProvider(h.svc)
	if *mode == "evse" {
		h.setupEVSE()
	} else if *mode == "lpc" {
		h.setupLPC()
	} else if *mode == "eg" {
		h.setupEG()
	} else {
		h.setupCEM()
	}
	if *trust != "" {
		h.svc.RegisterRemoteService(shipapi.NewServiceIdentity(*trust, "", ""))
	}
	if err := h.svc.Start(); err != nil {
		panic(err)
	}
	say("READY")

	scanner := bufio.NewScanner(os.Stdin)
	for scanner.Scan() {
		parts := strings.Fields(scanner.Text())
		if len(parts) == 0 {
			continue
		}
		switch parts[0] {
		case "plug":
			h.plug()
		case "unplug":
			h.unplug()
		case "measure":
			a, _ := strconv.ParseFloat(parts[1], 64)
			e, _ := strconv.ParseFloat(parts[2], 64)
			h.measure(a, e)
		case "power":
			p, _ := strconv.ParseFloat(parts[1], 64)
			h.updatePower(p)
		case "limit":
			v, _ := strconv.ParseFloat(parts[1], 64)
			d := time.Duration(0)
			if len(parts) > 2 {
				secs, _ := strconv.ParseFloat(parts[2], 64)
				d = time.Duration(secs * float64(time.Second))
			}
			h.egLimit = v
			h.egWrite(v, true, d)
		case "release":
			h.egWrite(h.egLimit, false, 0)
		case "quit":
			h.svc.Shutdown()
			return
		}
	}
	h.svc.Shutdown()
}
