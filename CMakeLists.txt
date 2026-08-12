# Top-level build for PyFlow.
#
# All CMake content lives here at the repository root: the crypto_api C
# library (sources under PyFlow/crypto_api, C tests under test/) plus its
# install/export rules. The Python package is managed by uv/pytest instead.
cmake_minimum_required(VERSION 3.16)
project(pyflow VERSION 0.1.0 LANGUAGES C)

include(GNUInstallDirs)
include(CMakePackageConfigHelpers)
include(CTest) # defines BUILD_TESTING

enable_testing()

option(CRYPTO_API_BUILD_TESTS "Build the crypto_api test suite" ON)

find_package(OpenSSL REQUIRED)

set(CRYPTO_API_VERSION 1.0.0)
set(CRYPTO_API_VERSION_MAJOR 1)

set(CRYPTO_API_SOURCES
  PyFlow/crypto_api/pf_crypto.c
  PyFlow/crypto_api/pf_rsa.c
  PyFlow/crypto_api/pf_ecdh.c
)

add_library(crypto_api SHARED ${CRYPTO_API_SOURCES})
add_library(crypto_api::crypto_api ALIAS crypto_api)

# Windows shared build needs the export macro (PF_CRYPTO_SHARED).
if(WIN32)
  target_compile_definitions(crypto_api PRIVATE PF_CRYPTO_SHARED)
endif()

target_include_directories(crypto_api PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/PyFlow/crypto_api/include>
  $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>
)
target_link_libraries(crypto_api PUBLIC OpenSSL::Crypto)

set_target_properties(crypto_api PROPERTIES
  C_STANDARD 11
  C_STANDARD_REQUIRED ON
  C_EXTENSIONS OFF
  C_VISIBILITY_PRESET hidden
  VERSION ${CRYPTO_API_VERSION}
  SOVERSION ${CRYPTO_API_VERSION_MAJOR}
  POSITION_INDEPENDENT_CODE ON
)

if(CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
  target_compile_options(crypto_api PRIVATE -Wall -Wextra)
endif()

# ---- Install & export -----------------------------------------------------
install(TARGETS crypto_api
  EXPORT crypto_apiTargets
  ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR}
  LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
  RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
)
install(DIRECTORY PyFlow/crypto_api/include/ DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})

install(EXPORT crypto_apiTargets
  FILE crypto_apiTargets.cmake
  NAMESPACE crypto_api::
  DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/crypto_api
)

configure_package_config_file(
  ${CMAKE_CURRENT_SOURCE_DIR}/cmake/crypto_apiConfig.cmake.in
  ${CMAKE_CURRENT_BINARY_DIR}/crypto_apiConfig.cmake
  INSTALL_DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/crypto_api
)
write_basic_package_version_file(
  ${CMAKE_CURRENT_BINARY_DIR}/crypto_apiConfigVersion.cmake
  VERSION ${CRYPTO_API_VERSION}
  COMPATIBILITY SameMajorVersion
)
install(FILES
  ${CMAKE_CURRENT_BINARY_DIR}/crypto_apiConfig.cmake
  ${CMAKE_CURRENT_BINARY_DIR}/crypto_apiConfigVersion.cmake
  DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/crypto_api
)

# pkg-config file
configure_file(${CMAKE_CURRENT_SOURCE_DIR}/crypto_api.pc.in
               ${CMAKE_CURRENT_BINARY_DIR}/crypto_api.pc @ONLY)
install(FILES ${CMAKE_CURRENT_BINARY_DIR}/crypto_api.pc
        DESTINATION ${CMAKE_INSTALL_LIBDIR}/pkgconfig)

# ---- C tests (sources in test/) -------------------------------------------
if(CRYPTO_API_BUILD_TESTS AND BUILD_TESTING)
  foreach(test_name IN ITEMS test_hkdf test_rsa test_ecdh)
    add_executable(${test_name} test/${test_name}.c)
    target_link_libraries(${test_name} PRIVATE crypto_api)
    target_compile_options(${test_name} PRIVATE -Wall -Wextra)
    add_test(NAME ${test_name} COMMAND ${test_name})
  endforeach()

  # Memory/UB sanity under ASan+UBSan when the compiler supports it.
  if(CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
    include(CheckCCompilerFlag)
    check_c_compiler_flag("-fsanitize=address,undefined" CRYPTO_API_HAS_SANITIZERS)
    if(CRYPTO_API_HAS_SANITIZERS)
      foreach(test_name IN ITEMS test_hkdf test_rsa test_ecdh)
        target_compile_options(${test_name} PRIVATE -fsanitize=address,undefined -fno-omit-frame-pointer)
        target_link_options(${test_name} PRIVATE -fsanitize=address,undefined)
      endforeach()
    endif()
  endif()
endif()
