ccflags-y += -DDEFAULT_SYMBOL_NAMESPACE=DEMETER
ccflags-y += -I$(srctree)/mm/PhaseTieing

obj-$(CONFIG_DEMETER) += PhaseTieing_placement.o

PhaseTieing_placement-objs += module.o sysfs.o core.o vector.o sandbox.o

# obj-$(CONFIG_DEMETER_TEST) += PhaseTieing_test.o

# PhaseTieing_test-objs +=

obj-$(CONFIG_DEMETER) += PhaseTieing_balloon.o

PhaseTieing_balloon-objs += balloon.o

