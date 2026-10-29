import sys
if '--adaptive' in sys.argv:
    sys.argv.remove('--adaptive')
    from adaptive_controller import main
else:
    from legacy_controller import main
main()
